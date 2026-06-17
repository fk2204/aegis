"""Sprint 7 — Close note-driven auto-status for funder_note_submissions.

When the Close webhook receives an ``activity.note`` ``created`` event
on a Lead that maps to an AEGIS merchant with exactly one pending
funder_note_submission, the handler:

  * Pattern-matches the note body to a status literal
    (approved/declined/countered).
  * Extracts an offer_amount when present (e.g. "$45,000", "45k").
  * Updates the submission row.
  * Writes a ``submission.auto_status_from_close_note`` audit row
    keyed off the Close activity id (idempotent on redelivery).
  * On ``approved``: posts a "Fund the deal" Close task.

Skip conditions covered:
  * No pending submission → silent skip.
  * Multiple pending submissions → ``submission.auto_status_ambiguous``
    audit row, no update (matcher can't pick a funder from free text).
  * Unknown lead → silent skip.
  * No decision phrase → silent skip.
  * Already-processed activity_id → silent skip.
  * Bare factor rates like ``1.35`` don't extract as money.
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
    get_funder_repository,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.api.routes.webhooks_close import (
    _extract_decision_from_note_text,
    _extract_offer_amount_from_note_text,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.funder_note_submissions import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"
_FUNDED_STATUS_ID = "stat_OXb0lwLgcuUwNqtm7S9FjdJxRIFtTRCbFdNZUURxcwh"
_DEAD_LENDER_STATUS_ID = "stat_jnyp9hrSneIA2b5z52Cj9EE9C98mEtlslfOlWzw7UTw"

_LEAD_ID = "lead_note_abc"


def _sign(timestamp: str, body: bytes) -> str:
    secret = bytes.fromhex(_TEST_SECRET_HEX)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _note_event(
    *,
    note_text: str,
    lead_id: str = _LEAD_ID,
    activity_id: str = "actib_note_001",
    event_id: str = "ev_note_001",
) -> dict[str, Any]:
    return {
        "event": {
            "id": event_id,
            "date_created": "2026-06-17T12:00:00+00:00",
            "action": "created",
            "object_type": "activity.note",
            "object_id": activity_id,
            "lead_id": lead_id,
            "organization_id": "orga_test",
            "user_id": "user_test",
            "changed_fields": [],
            "previous_data": {},
            "data": {
                "id": activity_id,
                "_type": "Note",
                "lead_id": lead_id,
                "note": note_text,
                "user_id": "user_test",
            },
            "meta": {},
            "request_id": "req_note_001",
        },
        "subscription_id": "whsub_notes",
    }


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
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture()
def close_post_log() -> list[httpx.Request]:
    return []


@pytest.fixture()
def stub_close_client(
    monkeypatch: pytest.MonkeyPatch,
    close_post_log: list[httpx.Request],
) -> CloseClient:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    monkeypatch.setenv("CLOSE_FUNDED_STATUS_ID", _FUNDED_STATUS_ID)
    monkeypatch.setenv("CLOSE_DEAD_LENDER_STATUS_ID", _DEAD_LENDER_STATUS_ID)
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/task/"):
            close_post_log.append(request)
            return httpx.Response(201, json={"id": "task_xyz"})
        return httpx.Response(200, json={"id": _LEAD_ID})

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture()
def client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    stub_close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_note_submission_repository] = lambda: submissions_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_close_client] = lambda: stub_close_client
    with TestClient(app) as tc:
        yield tc
    reset_dependency_caches()


def _post_signed(client: TestClient, body_dict: dict[str, Any]) -> Any:
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


def _seed_one_pending_sub(
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
) -> tuple[MerchantRow, UUID, FunderRow]:
    merchant = repo.upsert(
        MerchantRow(
            business_name="Note Logistics Co",
            owner_name="J Doe",
            state="CA",
            close_lead_id=_LEAD_ID,
            status="finalized",
        )
    )
    funder = funder_repo.upsert(FunderRow(name="Kapitus"))
    sub = submissions_repo.create(
        merchant_id=merchant.id,
        funder_id=funder.id,
        funder_note="prior submission",
        submitted_by="op@commerafunding.com",
    )
    return merchant, sub.id, funder


# ---------------------------------------------------------------------------
# Pure-function extractors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("APPROVED for $45k", "approved"),
        ("Kapitus approved for 45000", "approved"),
        ("they approved at 1.35", "approved"),
        ("DECLINED — too many positions", "declined"),
        ("declined by lender", "declined"),
        ("not approved at this time", "declined"),
        ("COUNTERED at 50k", "countered"),
        ("counter offer 30000", "countered"),
        ("counter at 1.40", "countered"),
        ("file looks good, sending now", None),
        ("their CPA reviewed the financials", None),
    ],
)
def test_extract_decision_from_note_text(text: str, expected: str | None) -> None:
    assert _extract_decision_from_note_text(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("approved for $45,000", "45000"),
        ("approved for 45k", "45000"),
        ("approved for $45.5k", "45500.0"),
        ("approved for 1.5M", "1500000.0"),
        ("approved for $250,000.50", "250000.50"),
        # Factor rate alone — must NOT read as money.
        ("approved at 1.35", None),
        # Bare 2-3 digit integer — also not money.
        ("file age 60", None),
        ("approved", None),
    ],
)
def test_extract_offer_amount_from_note_text(text: str, expected: str | None) -> None:
    from decimal import Decimal

    got = _extract_offer_amount_from_note_text(text)
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert got == Decimal(expected)


# ---------------------------------------------------------------------------
# End-to-end webhook flow
# ---------------------------------------------------------------------------


def test_approved_note_updates_submission_and_creates_fund_task(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    close_post_log: list[httpx.Request],
) -> None:
    _, sub_id, _ = _seed_one_pending_sub(repo, submissions_repo, funder_repo)

    resp = _post_signed(client, _note_event(note_text="Kapitus approved for $45,000"))
    assert resp.status_code == 204

    updated = submissions_repo.get(sub_id)
    assert updated.status == "approved"
    from decimal import Decimal

    assert updated.offer_amount == Decimal("45000")

    auto_rows = [
        e for e in audit.entries if e["action"] == "submission.auto_status_from_close_note"
    ]
    assert len(auto_rows) == 1
    details = auto_rows[0]["details"]
    assert details["matched_status"] == "approved"
    assert details["activity_id"] == "actib_note_001"
    assert details["offer_amount"] == "45000"

    # "Fund the deal" Close task fired.
    task_calls = [r for r in close_post_log if r.url.path.endswith("/task/")]
    assert len(task_calls) == 1
    body = json.loads(task_calls[0].read())
    assert "Fund the deal" in body["text"]
    assert "Note Logistics Co" in body["text"]
    assert "Kapitus" in body["text"]
    assert "$45,000.00" in body["text"]

    fund_rows = [e for e in audit.entries if e["action"] == "close.task.fund_the_deal_created"]
    assert len(fund_rows) == 1


def test_declined_note_updates_submission_without_creating_task(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    close_post_log: list[httpx.Request],
) -> None:
    _, sub_id, _ = _seed_one_pending_sub(repo, submissions_repo, funder_repo)

    resp = _post_signed(client, _note_event(note_text="declined by lender — stacked"))
    assert resp.status_code == 204

    assert submissions_repo.get(sub_id).status == "declined"
    assert [r for r in close_post_log if r.url.path.endswith("/task/")] == []


def test_no_pending_submission_silent_skip(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_post_log: list[httpx.Request],
) -> None:
    # Merchant exists but has no pending submission.
    repo.upsert(
        MerchantRow(
            business_name="Empty Co",
            owner_name="J Doe",
            state="CA",
            close_lead_id=_LEAD_ID,
            status="finalized",
        )
    )

    resp = _post_signed(client, _note_event(note_text="APPROVED for 45k"))
    assert resp.status_code == 204

    assert not any(e["action"] == "submission.auto_status_from_close_note" for e in audit.entries)
    assert close_post_log == []


def test_two_pending_submissions_emits_ambiguous_and_skips(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
) -> None:
    merchant, _, _ = _seed_one_pending_sub(repo, submissions_repo, funder_repo)
    # Add a second pending submission against a different funder.
    other_funder = funder_repo.upsert(FunderRow(name="Velocity"))
    submissions_repo.create(
        merchant_id=merchant.id,
        funder_id=other_funder.id,
        funder_note="second pending",
        submitted_by="op@commerafunding.com",
    )

    resp = _post_signed(client, _note_event(note_text="approved for 45k"))
    assert resp.status_code == 204

    # Neither submission updated.
    rows = submissions_repo.list_for_merchant(merchant.id)
    assert all(r.status == "pending" for r in rows)

    # Ambiguity audit row written.
    ambig = [e for e in audit.entries if e["action"] == "submission.auto_status_ambiguous"]
    assert len(ambig) == 1
    assert ambig[0]["details"]["matched_status"] == "approved"
    assert len(ambig[0]["details"]["pending_submission_ids"]) == 2


def test_unknown_lead_silent_skip(
    client: TestClient,
    audit: InMemoryAuditLog,
    close_post_log: list[httpx.Request],
) -> None:
    resp = _post_signed(
        client, _note_event(note_text="approved for 45k", lead_id="lead_does_not_exist")
    )
    assert resp.status_code == 204
    assert not any(e["action"] == "submission.auto_status_from_close_note" for e in audit.entries)


def test_note_without_decision_phrase_silent_skip(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
) -> None:
    _, sub_id, _ = _seed_one_pending_sub(repo, submissions_repo, funder_repo)
    resp = _post_signed(client, _note_event(note_text="left a voicemail — will follow up tomorrow"))
    assert resp.status_code == 204
    assert submissions_repo.get(sub_id).status == "pending"


def test_redelivery_of_same_activity_does_not_double_update(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
    close_post_log: list[httpx.Request],
) -> None:
    _seed_one_pending_sub(repo, submissions_repo, funder_repo)
    event = _note_event(note_text="approved for 45k", activity_id="actib_redeliver")

    resp1 = _post_signed(client, event)
    assert resp1.status_code == 204
    resp2 = _post_signed(client, event)
    assert resp2.status_code == 204

    auto_rows = [
        e for e in audit.entries if e["action"] == "submission.auto_status_from_close_note"
    ]
    assert len(auto_rows) == 1
    task_calls = [r for r in close_post_log if r.url.path.endswith("/task/")]
    assert len(task_calls) == 1


def test_factor_rate_in_note_does_not_extract_as_amount(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
) -> None:
    _, sub_id, _ = _seed_one_pending_sub(repo, submissions_repo, funder_repo)
    resp = _post_signed(client, _note_event(note_text="approved at 1.35"))
    assert resp.status_code == 204
    assert submissions_repo.get(sub_id).status == "approved"
    auto_entries = [
        e for e in audit.entries if e["action"] == "submission.auto_status_from_close_note"
    ]
    assert auto_entries[0]["details"]["offer_amount"] is None


def test_non_note_event_does_not_trigger_handler(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    submissions_repo: InMemoryFunderNoteSubmissionRepository,
    funder_repo: InMemoryFunderRepository,
    audit: InMemoryAuditLog,
) -> None:
    """An opportunity.updated event with status changes must NOT trigger
    the note handler (which would crash on the missing note field)."""
    _, sub_id, _ = _seed_one_pending_sub(repo, submissions_repo, funder_repo)
    body = {
        "event": {
            "id": "ev_opp",
            "action": "updated",
            "object_type": "opportunity",
            "object_id": "oppo_abc",
            "lead_id": _LEAD_ID,
            "changed_fields": ["status_id"],
            "previous_data": {"status_id": "old"},
            "data": {"status_id": "new"},
        },
        "subscription_id": "whsub_opportunities",
    }
    resp = _post_signed(client, body)
    assert resp.status_code == 204
    assert submissions_repo.get(sub_id).status == "pending"
