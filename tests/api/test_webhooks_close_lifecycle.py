"""Sprint 4 Feature 3 — Close lifecycle audit + submission sync.

The webhook handler now writes a ``deal.close_status_changed`` audit
row whenever an opportunity status changes and forward-syncs the
merchant's pending ``funder_note_submissions`` when the new status
maps to a funder decision:

* ``Funded``        -> ``approved``
* ``Dead - Lender`` -> ``declined``

Tests use the same HMAC signing + canned-Lead transport as the
sibling Pre-UW trigger suite (``test_webhooks_close.py``); these
exercise the lifecycle path independently and confirm both
audit-only and cascade-to-submission behaviours.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_funder_note_submission_repository,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.funder_note_submissions import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"

# Verified 2026-06-15 against the live Commera Sales pipeline via the
# Close MCP. Tests pin to those exact IDs so a config drift in
# aegis.config trips an assertion here before it ships.
_FUNDED_STATUS_ID = "stat_OXb0lwLgcuUwNqtm7S9FjdJxRIFtTRCbFdNZUURxcwh"
_DEAD_LENDER_STATUS_ID = "stat_jnyp9hrSneIA2b5z52Cj9EE9C98mEtlslfOlWzw7UTw"
_DEAD_MERCHANT_STATUS_ID = "stat_oYB8WSjysFxYGdpKVJILsBWEGe1P9EFP21vuh25SaQL"

_LEAD_ID = "lead_lifecycle_abc"


def _sign(timestamp: str, body: bytes, secret_hex: str = _TEST_SECRET_HEX) -> str:
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _opportunity_status_event(
    *,
    lead_id: str = _LEAD_ID,
    new_status_id: str,
    previous_status_id: str = _TRIGGER_STATUS_ID,
    event_id: str = "ev_lifecycle_001",
) -> dict[str, Any]:
    return {
        "event": {
            "id": event_id,
            "date_created": "2026-06-15T12:00:00+00:00",
            "action": "updated",
            "object_type": "opportunity",
            "object_id": "oppo_lifecycle",
            "lead_id": lead_id,
            "organization_id": "orga_test",
            "user_id": "user_test",
            "changed_fields": ["status_id", "status_label", "date_status_changed"],
            "previous_data": {"status_id": previous_status_id},
            "data": {"status_id": new_status_id},
            "meta": {},
            "request_id": "req_lifecycle_001",
        },
        "subscription_id": "whsub_lifecycle",
    }


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def submissions_repo() -> InMemoryFunderNoteSubmissionRepository:
    return InMemoryFunderNoteSubmissionRepository()


@pytest.fixture()
def stub_close_client(monkeypatch: pytest.MonkeyPatch) -> CloseClient:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    monkeypatch.setenv("CLOSE_FUNDED_STATUS_ID", _FUNDED_STATUS_ID)
    monkeypatch.setenv("CLOSE_DEAD_LENDER_STATUS_ID", _DEAD_LENDER_STATUS_ID)
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        # No lead GET is expected on the lifecycle path -- the Pre-UW
        # branch is the only thing that calls get_lead, and the
        # lifecycle tests never trip that branch. If a test does fall
        # through to the Pre-UW handler, returning an empty lead would
        # explode loudly, which is what we want.
        return httpx.Response(200, json={"id": _LEAD_ID})

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture()
def client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    stub_close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: submissions_repo
    app.dependency_overrides[get_close_client] = lambda: stub_close_client
    with TestClient(app) as tc:
        yield tc
    reset_dependency_caches()


def _post_signed(
    client: TestClient,
    body_dict: dict[str, Any],
) -> Any:
    timestamp = str(int(time.time()))
    raw = json.dumps(body_dict).encode("utf-8")
    sig = _sign(timestamp, raw)
    return client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": timestamp,
        },
    )


def _seed_merchant_with_pending_subs(
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    *,
    n: int = 2,
    close_lead_id: str = _LEAD_ID,
) -> tuple[MerchantRow, list[UUID]]:
    merchant = repo.upsert(
        MerchantRow(
            business_name="Lifecycle Co",
            owner_name="J Doe",
            state="CA",
            close_lead_id=close_lead_id,
            status="finalized",
        )
    )
    sub_ids: list[UUID] = []
    for i in range(n):
        sub = submissions_repo.create(
            merchant_id=merchant.id,
            funder_id=UUID(int=i + 1),
            funder_note="prior submission",
            submitted_by="op@commerafunding.com",
        )
        sub_ids.append(sub.id)
    return merchant, sub_ids


def _lifecycle_audit_rows(audit: InMemoryAuditLog) -> list[dict[str, Any]]:
    return [r for r in audit.entries if r["action"] == "deal.close_status_changed"]


# ----------------------------------------------------------------------
# Cases
# ----------------------------------------------------------------------


def test_funded_status_syncs_pending_submissions_to_approved(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    merchant, sub_ids = _seed_merchant_with_pending_subs(repo, submissions_repo, n=3)
    resp = _post_signed(
        client,
        _opportunity_status_event(new_status_id=_FUNDED_STATUS_ID),
    )
    assert resp.status_code == 204, resp.text

    # All pending submissions are now approved.
    for sub_id in sub_ids:
        row = submissions_repo.get(sub_id)
        assert row.status == "approved"
        assert row.responded_at is not None

    # Single lifecycle audit row with the synced ids surfaced.
    lifecycle_rows = _lifecycle_audit_rows(audit)
    assert len(lifecycle_rows) == 1
    details = lifecycle_rows[0]["details"]
    assert details["new_status_id"] == _FUNDED_STATUS_ID
    assert details["synced_submission_status"] == "approved"
    assert details["merchant_id"] == str(merchant.id)
    assert set(details["synced_submission_ids"]) == {str(s) for s in sub_ids}


def test_dead_lender_status_syncs_pending_submissions_to_declined(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    _merchant, sub_ids = _seed_merchant_with_pending_subs(repo, submissions_repo, n=2)
    resp = _post_signed(
        client,
        _opportunity_status_event(new_status_id=_DEAD_LENDER_STATUS_ID),
    )
    assert resp.status_code == 204, resp.text

    for sub_id in sub_ids:
        assert submissions_repo.get(sub_id).status == "declined"

    lifecycle_rows = _lifecycle_audit_rows(audit)
    assert len(lifecycle_rows) == 1
    assert lifecycle_rows[0]["details"]["synced_submission_status"] == "declined"


def test_dead_merchant_status_audits_but_does_not_sync(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    """``Dead - Merchant`` reflects a merchant walk-away, NOT a funder
    decision. The lifecycle audit row still fires so the dossier
    history surface can record the transition, but the pending
    submissions stay pending."""
    _merchant, sub_ids = _seed_merchant_with_pending_subs(repo, submissions_repo, n=2)
    resp = _post_signed(
        client,
        _opportunity_status_event(new_status_id=_DEAD_MERCHANT_STATUS_ID),
    )
    assert resp.status_code == 204, resp.text

    for sub_id in sub_ids:
        assert submissions_repo.get(sub_id).status == "pending"

    lifecycle_rows = _lifecycle_audit_rows(audit)
    assert len(lifecycle_rows) == 1
    details = lifecycle_rows[0]["details"]
    assert details["new_status_id"] == _DEAD_MERCHANT_STATUS_ID
    assert details["synced_submission_status"] is None
    assert details["synced_submission_ids"] == []


def test_pre_uw_trigger_writes_lifecycle_audit_without_submission_sync(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Pre-UW is the underwriting-start status -- it's not a funder
    decision. The existing trigger pipeline still fires (Lead fetch +
    merchant upsert + orchestration enqueue), AND the new lifecycle
    branch writes its audit row. The pending submissions don't
    move because Pre-UW doesn't map to approved/declined."""
    _merchant, sub_ids = _seed_merchant_with_pending_subs(repo, submissions_repo, n=1)
    resp = _post_signed(
        client,
        _opportunity_status_event(
            new_status_id=_TRIGGER_STATUS_ID,
            previous_status_id="stat_priorActiveStage",
        ),
    )
    # The Pre-UW path needs a fuller lead payload than our stub returns
    # to populate every field_map'd attribute; we don't care whether the
    # full Pre-UW path succeeded here, only that the lifecycle audit
    # row landed BEFORE that downstream pipeline ran. Treat any 2xx /
    # 4xx as proof the lifecycle handler ran; only 5xx would mean the
    # webhook framework itself blew up before reaching our code.
    assert resp.status_code < 500, resp.text

    assert submissions_repo.get(sub_ids[0]).status == "pending"

    lifecycle_rows = _lifecycle_audit_rows(audit)
    assert len(lifecycle_rows) == 1
    details = lifecycle_rows[0]["details"]
    assert details["new_status_id"] == _TRIGGER_STATUS_ID
    assert details["synced_submission_status"] is None


def test_already_non_pending_submissions_are_not_touched(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
) -> None:
    """Forward-only sync: a submission the operator already marked
    ``approved`` by hand should NOT be flipped to ``declined`` by a
    later Dead - Lender webhook (and vice versa). Protects the
    history surface from a status flap on the Close side rewriting
    operator-set ground truth."""
    _merchant, sub_ids = _seed_merchant_with_pending_subs(repo, submissions_repo, n=2)
    # Operator already marked the first row approved.
    submissions_repo.update_status(sub_ids[0], status="approved")

    resp = _post_signed(
        client,
        _opportunity_status_event(new_status_id=_DEAD_LENDER_STATUS_ID),
    )
    assert resp.status_code == 204, resp.text

    # First row stayed approved (operator decision wins).
    assert submissions_repo.get(sub_ids[0]).status == "approved"
    # Second row was pending, now declined.
    assert submissions_repo.get(sub_ids[1]).status == "declined"


def test_unknown_lead_writes_audit_with_null_merchant_id(
    client: TestClient,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A status change for a lead AEGIS never saw still leaves a
    receipt: ``merchant_id: null``, no synced submissions, but the
    audit row is enough for the operator to grep ``deal.close_status_changed``
    and find orphans."""
    resp = _post_signed(
        client,
        _opportunity_status_event(
            lead_id="lead_never_seen",
            new_status_id=_FUNDED_STATUS_ID,
        ),
    )
    assert resp.status_code == 204, resp.text

    lifecycle_rows = _lifecycle_audit_rows(audit)
    assert len(lifecycle_rows) == 1
    details = lifecycle_rows[0]["details"]
    assert details["merchant_id"] is None
    # The classifier still fires -- ``Funded`` maps to ``approved`` --
    # but because no merchant resolved, no submissions could sync.
    assert details["synced_submission_status"] == "approved"
    assert details["synced_submission_ids"] == []


def test_no_lifecycle_audit_when_status_id_not_in_changed_fields(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Opportunity updates that DON'T change status_id (e.g. just
    confidence or note edit) must not produce a lifecycle audit row."""
    _merchant, _sub_ids = _seed_merchant_with_pending_subs(repo, submissions_repo, n=1)
    event = _opportunity_status_event(new_status_id=_FUNDED_STATUS_ID)
    event["event"]["changed_fields"] = ["confidence"]
    resp = _post_signed(client, event)
    assert resp.status_code == 204, resp.text

    assert _lifecycle_audit_rows(audit) == []


def test_redelivery_does_not_double_flip_already_synced_submissions(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Close retries within 72 hours on a non-2xx (and occasionally
    on success). Two identical Funded events should produce two
    lifecycle audit rows (each delivery deserves its own receipt) but
    the SECOND row's ``synced_submission_ids`` must be empty because
    the submissions are already approved."""
    _merchant, sub_ids = _seed_merchant_with_pending_subs(repo, submissions_repo, n=2)
    event = _opportunity_status_event(new_status_id=_FUNDED_STATUS_ID)

    resp1 = _post_signed(client, event)
    assert resp1.status_code == 204
    resp2 = _post_signed(client, event)
    assert resp2.status_code == 204

    lifecycle_rows = _lifecycle_audit_rows(audit)
    assert len(lifecycle_rows) == 2
    # First delivery synced both submissions.
    assert set(lifecycle_rows[0]["details"]["synced_submission_ids"]) == {str(s) for s in sub_ids}
    # Second delivery synced nothing -- both rows were already approved.
    assert lifecycle_rows[1]["details"]["synced_submission_ids"] == []

    for sub_id in sub_ids:
        assert submissions_repo.get(sub_id).status == "approved"
