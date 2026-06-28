"""Tests for ``aegis.business_intel.sos_refresh`` — persistence orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.business_intel.sos_checker import SOSResult
from aegis.business_intel.sos_refresh import (
    SOS_CHECK_TTL,
    ensure_sos_check,
    refresh_sos_for_merchant,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    MerchantNotFoundError,
)


def _make_merchant(
    *,
    sos_checked_at: datetime | None = None,
    business_name: str = "Acme LLC",
    state: str = "FL",
) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name=business_name,
        state=state,
        sos_checked_at=sos_checked_at,
    )


class _StubChecker:
    def __init__(self, result: SOSResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str | None]] = []

    def check_entity(self, business_name: str, state: str | None) -> SOSResult:
        self.calls.append((business_name, state))
        return self.result


def _ok_result(*, is_active: bool = True, source: str = "local_db:FL") -> SOSResult:
    return SOSResult(
        found=True,
        status="ACTIVE" if is_active else "DISSOLVED",
        entity_name="Acme LLC",
        formation_date="2018-04-12",
        is_active=is_active,
        data_source=source,
        checked_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# refresh_sos_for_merchant
# ---------------------------------------------------------------------------
def test_refresh_persists_fields_and_writes_audit() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    result = refresh_sos_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        checker=_StubChecker(_ok_result()),
    )
    assert result.found is True
    assert result.is_active is True

    refreshed = repo.get(merchant.id)
    assert refreshed.sos_status == "ACTIVE"
    assert refreshed.sos_entity_name == "Acme LLC"
    assert refreshed.sos_formation_date == "2018-04-12"
    assert refreshed.sos_is_active is True
    assert refreshed.sos_data_source == "local_db:FL"
    assert refreshed.sos_state_checked == "FL"
    assert refreshed.sos_checked_at is not None

    row = next(e for e in audit.entries if e["action"] == "merchant.sos_check.completed")
    assert row["details"]["found"] is True
    assert row["details"]["is_active"] is True
    assert row["details"]["data_source"] == "local_db:FL"


def test_refresh_audit_carries_counts_not_entity_name() -> None:
    """CLAUDE.md PII rule — entity_name lives on the merchants row, not
    in audit details. The audit captures source + boolean only."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant(business_name="Confidential Holdings LLC")
    repo.upsert(merchant)

    refresh_sos_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        checker=_StubChecker(
            SOSResult(
                found=True,
                status="ACTIVE",
                entity_name="Confidential Holdings LLC",
                formation_date="2020-01-01",
                is_active=True,
                data_source="local_db:FL",
                checked_at=datetime.now(UTC),
            )
        ),
    )
    row = next(e for e in audit.entries if e["action"] == "merchant.sos_check.completed")
    details_str = str(row["details"])
    assert "Confidential Holdings" not in details_str


def test_refresh_unknown_merchant_raises() -> None:
    with pytest.raises(MerchantNotFoundError):
        refresh_sos_for_merchant(
            uuid4(),
            merchants_repo=InMemoryMerchantRepository(),
            audit=InMemoryAuditLog(),
            checker=_StubChecker(_ok_result()),
        )


# ---------------------------------------------------------------------------
# ensure_sos_check — 30-day TTL
# ---------------------------------------------------------------------------
def test_ensure_skips_when_recently_checked() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    recent = datetime.now(UTC) - timedelta(days=5)
    merchant = _make_merchant(sos_checked_at=recent)
    repo.upsert(merchant)

    checker = _StubChecker(_ok_result())
    result_row = ensure_sos_check(merchant, merchants_repo=repo, audit=audit, checker=checker)
    assert checker.calls == []
    assert result_row.sos_checked_at == recent
    assert audit.entries == []


def test_ensure_runs_when_ttl_expired() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    stale = datetime.now(UTC) - SOS_CHECK_TTL - timedelta(days=1)
    merchant = _make_merchant(sos_checked_at=stale)
    repo.upsert(merchant)

    checker = _StubChecker(_ok_result())
    refreshed = ensure_sos_check(merchant, merchants_repo=repo, audit=audit, checker=checker)
    assert checker.calls == [("Acme LLC", "FL")]
    assert refreshed.sos_checked_at != stale
    assert any(e["action"] == "merchant.sos_check.completed" for e in audit.entries)


def test_ensure_runs_when_never_checked() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant(sos_checked_at=None)
    repo.upsert(merchant)

    checker = _StubChecker(_ok_result())
    ensure_sos_check(merchant, merchants_repo=repo, audit=audit, checker=checker)
    assert len(checker.calls) == 1


def test_ensure_handles_unknown_merchant_gracefully() -> None:
    """If the merchant somehow disappears between ensure_sos_check and
    repository.get, the wrapper logs + returns the original merchant
    rather than raising into the caller (which would block scoring)."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    # Construct a merchant but never save to repo.
    orphan = _make_merchant(sos_checked_at=None)

    checker = _StubChecker(_ok_result())
    result = ensure_sos_check(orphan, merchants_repo=repo, audit=audit, checker=checker)
    assert result is orphan
    assert audit.entries == []
