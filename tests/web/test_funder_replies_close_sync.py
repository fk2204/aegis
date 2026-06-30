"""Unit tests for the Close-outbound sync helper on the funder-replies route.

Covers ``aegis.web.routers.funder_replies._sync_outcome_to_close`` —
the helper that posts a Close note + flips the opportunity status_id
after a successful ``record_outcome``. The helper is best-effort: it
must never raise (the upstream operator capture is the authoritative
write), and every Close-side failure should land an audit row.

Tests pin the contract for:

* ``approved`` outcome on a merchant with both ``close_lead_id`` and
  ``close_opportunity_id`` — fires ``post_note`` + ``update_opportunity_status``
  using ``settings.close_funded_status_id``.
* ``declined`` outcome — fires the same two writes but with
  ``settings.close_dead_lender_status_id``.
* ``countered`` outcome — posts the note but skips the status flip
  (still under negotiation, opportunity status should not move).
* Missing ``close_lead_id`` — no Close calls at all, no audit rows.
* ``CloseError`` from the API — audit row ``close.outcome_sync.failed``
  with the right ``stage`` value, no re-raise.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseError
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.web.routers.funder_replies import _sync_outcome_to_close


class _StubCloseClient:
    """Records every call without touching the network.

    Mirrors the real ``CloseClient`` surface used by
    ``_sync_outcome_to_close`` — only the two methods the helper
    invokes are implemented. Each call lands on the public ``calls``
    list so tests can assert the (kind, args) tuple.
    """

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._raise_on = raise_on

    def post_note(self, lead_id: str, note_text: str) -> dict[str, Any]:
        if self._raise_on == "note":
            raise CloseError("simulated note failure")
        self.calls.append(("post_note", (lead_id, note_text)))
        return {"id": "actv_test"}

    def update_opportunity_status(
        self, opportunity_id: str, status_id: str
    ) -> dict[str, Any]:
        if self._raise_on == "status":
            raise CloseError("simulated status failure")
        self.calls.append(("update_opportunity_status", (opportunity_id, status_id)))
        return {"id": opportunity_id}


def _merchant(*, with_close: bool = True) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name="Test Co",
        state="NY",
        close_lead_id="lead_TEST" if with_close else None,
        close_opportunity_id="oppo_TEST" if with_close else None,
    )


def _seed_merchant(merchant: MerchantRow) -> InMemoryMerchantRepository:
    repo = InMemoryMerchantRepository()
    repo.upsert(merchant)
    return repo


def _funder(funder_id: UUID) -> FunderRow:
    return FunderRow(
        id=funder_id,
        name="Test Funder",
        active=True,
    )


def _funder_repo(funder: FunderRow) -> InMemoryFunderRepository:
    repo = InMemoryFunderRepository()
    repo.upsert(funder)
    return repo


def _kwargs(
    *,
    merchant: MerchantRow,
    funder: FunderRow,
    outcome: str,
    merchants_repo: InMemoryMerchantRepository,
    funder_repo: InMemoryFunderRepository,
    close_client: _StubCloseClient | None,
    audit: InMemoryAuditLog,
) -> dict[str, Any]:
    return dict(
        merchant_id=merchant.id,
        funder_id=funder.id,
        submission_id=uuid4(),
        outcome=outcome,
        outcome_amount=Decimal("50000.00") if outcome in ("approved", "countered") else None,
        outcome_factor_rate=Decimal("1.30") if outcome in ("approved", "countered") else None,
        outcome_term_days=120 if outcome in ("approved", "countered") else None,
        outcome_notes="phone confirmation",
        merchants_repo=merchants_repo,
        funder_repo=funder_repo,
        close_client=close_client,
        audit=audit,
        actor_email="filip@commerafunding.com",
    )


def test_approved_outcome_posts_note_and_flips_status_to_funded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOSE_FUNDED_STATUS_ID", "stat_FUNDED_TEST")
    monkeypatch.setenv("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")
    from aegis.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    merchant = _merchant()
    funder = _funder(uuid4())
    audit = InMemoryAuditLog()
    client = _StubCloseClient()
    _sync_outcome_to_close(
        **_kwargs(
            merchant=merchant,
            funder=funder,
            outcome="approved",
            merchants_repo=_seed_merchant(merchant),
            funder_repo=_funder_repo(funder),
            close_client=client,
            audit=audit,
        ),
    )
    kinds = [c[0] for c in client.calls]
    assert kinds == ["post_note", "update_opportunity_status"]
    assert client.calls[0][1][0] == "lead_TEST"
    assert client.calls[1][1] == ("oppo_TEST", "stat_FUNDED_TEST")
    action_set = {e["action"] for e in audit.entries}
    assert "close.outcome_note_posted" in action_set
    assert "close.opportunity_status_synced" in action_set


def test_declined_outcome_flips_status_to_dead_lender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOSE_DEAD_LENDER_STATUS_ID", "stat_DEAD_TEST")
    monkeypatch.setenv("AEGIS_DATA_RESIDENCY_CONFIRMED", "true")
    from aegis.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    merchant = _merchant()
    funder = _funder(uuid4())
    audit = InMemoryAuditLog()
    client = _StubCloseClient()
    _sync_outcome_to_close(
        **_kwargs(
            merchant=merchant,
            funder=funder,
            outcome="declined",
            merchants_repo=_seed_merchant(merchant),
            funder_repo=_funder_repo(funder),
            close_client=client,
            audit=audit,
        ),
    )
    assert client.calls[1][1] == ("oppo_TEST", "stat_DEAD_TEST")


def test_countered_outcome_posts_note_but_does_not_flip_status() -> None:
    merchant = _merchant()
    funder = _funder(uuid4())
    audit = InMemoryAuditLog()
    client = _StubCloseClient()
    _sync_outcome_to_close(
        **_kwargs(
            merchant=merchant,
            funder=funder,
            outcome="countered",
            merchants_repo=_seed_merchant(merchant),
            funder_repo=_funder_repo(funder),
            close_client=client,
            audit=audit,
        ),
    )
    assert [c[0] for c in client.calls] == ["post_note"]
    actions = {e["action"] for e in audit.entries}
    assert "close.opportunity_status_synced" not in actions


def test_no_close_lead_id_skips_all_writes() -> None:
    merchant = _merchant(with_close=False)
    funder = _funder(uuid4())
    audit = InMemoryAuditLog()
    client = _StubCloseClient()
    _sync_outcome_to_close(
        **_kwargs(
            merchant=merchant,
            funder=funder,
            outcome="approved",
            merchants_repo=_seed_merchant(merchant),
            funder_repo=_funder_repo(funder),
            close_client=client,
            audit=audit,
        ),
    )
    assert client.calls == []
    assert audit.entries == []


def test_close_client_none_is_a_noop() -> None:
    merchant = _merchant()
    funder = _funder(uuid4())
    audit = InMemoryAuditLog()
    _sync_outcome_to_close(
        **_kwargs(
            merchant=merchant,
            funder=funder,
            outcome="approved",
            merchants_repo=_seed_merchant(merchant),
            funder_repo=_funder_repo(funder),
            close_client=None,
            audit=audit,
        ),
    )
    assert audit.entries == []


def test_close_note_post_failure_audits_and_continues() -> None:
    merchant = _merchant()
    funder = _funder(uuid4())
    audit = InMemoryAuditLog()
    client = _StubCloseClient(raise_on="note")
    _sync_outcome_to_close(
        **_kwargs(
            merchant=merchant,
            funder=funder,
            outcome="approved",
            merchants_repo=_seed_merchant(merchant),
            funder_repo=_funder_repo(funder),
            close_client=client,
            audit=audit,
        ),
    )
    failures = [e for e in audit.entries if e["action"] == "close.outcome_sync.failed"]
    assert len(failures) == 1
    assert failures[0]["details"]["stage"] == "note"
