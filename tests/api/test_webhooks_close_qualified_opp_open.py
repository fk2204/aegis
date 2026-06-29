"""Phase 2 / item 7.2 — Close lead.updated → "Qualified - Opp Open"
auto-creates an AEGIS merchant row before any PDF arrives.

Coverage:

* Status change to Qualified-Opp-Open with no existing merchant
  → merchant created + ``close.webhook.merchant_auto_created`` audit.
* Same status change with an existing merchant (redelivery /
  re-qualification) → no-op + ``close.webhook.status_change_no_op_existing_merchant``.
* Status change to a non-Qualified status → handler skips entirely
  (writes ``close.webhook.qualified_skipped`` with reason=wrong_status).
* lead.updated event without ``status_id`` in changed_fields → handler
  skips (writes ``close.webhook.qualified_skipped`` with
  reason=no_status_change).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.close.field_map import CLOSE_FIELD_IDS
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository

# Hex-encoded webhook secret. Mirrors what Close returns from the
# subscription POST response (signature_key). Tests do not exercise
# real Close traffic.
_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub, not a real secret

# The status ID the operator pins into ``CLOSE_QUALIFIED_OPP_OPEN_STATUS_ID``.
# Synthetic test value; production overrides via env. The handler reads
# whatever ``settings.close_qualified_opp_open_status_id`` resolves to.
_QUALIFIED_STATUS_ID = "stat_qualified_opp_open_test_id"
_OTHER_STATUS_ID = "stat_some_other_lead_status_test_id"

_LEAD_ID = "lead_qualified_test_001"


def _sign(timestamp: str, body: bytes, secret_hex: str = _TEST_SECRET_HEX) -> str:
    """Compute the close-sig-hash value Close would send."""
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _lead_payload(close_lead_id: str = _LEAD_ID) -> dict[str, Any]:
    """Canned Close Lead GET response covering the AEGIS-relevant
    custom fields. Mirrors the shape used in the sibling Pre-UW tests
    so the field-map path runs identically."""
    return {
        "id": close_lead_id,
        "display_name": "Qualified Lead Co.",
        f"custom.{CLOSE_FIELD_IDS['legal_name']}": "Qualified Lead LLC",
        f"custom.{CLOSE_FIELD_IDS['dba_name']}": "Qualified Lead",
        f"custom.{CLOSE_FIELD_IDS['ein']}": "98-7654321",
        f"custom.{CLOSE_FIELD_IDS['owner_name']}": "Alex Operator",
        f"custom.{CLOSE_FIELD_IDS['state']}": "FL",
        f"custom.{CLOSE_FIELD_IDS['industry']}": "Restaurant / Food Service",
        f"custom.{CLOSE_FIELD_IDS['naics_code']}": "722511",
        f"custom.{CLOSE_FIELD_IDS['time_in_business_months']}": 48,
        f"custom.{CLOSE_FIELD_IDS['fico_range']}": "650-699",
        f"custom.{CLOSE_FIELD_IDS['requested_amount']}": "$60,000.00",
        f"custom.{CLOSE_FIELD_IDS['entity_type_a']}": "LLC",
        f"custom.{CLOSE_FIELD_IDS['entity_type_b']}": "LLC",
    }


def _lead_status_event(
    *,
    lead_id: str = _LEAD_ID,
    new_status_id: str = _QUALIFIED_STATUS_ID,
    changed_fields: list[str] | None = None,
    action: str = "updated",
    event_id: str = "ev_qualified_001",
) -> dict[str, Any]:
    """Build a Close webhook event payload for a ``lead.updated`` with
    a status_id transition. Mirrors the live Close event shape verified
    against the org-side activity feed (object_type='lead',
    object_id=<lead_id>, data.status_id=<new>)."""
    return {
        "event": {
            "id": event_id,
            "date_created": "2026-06-28T12:00:00+00:00",
            "action": action,
            "object_type": "lead",
            "object_id": lead_id,
            "lead_id": lead_id,
            "organization_id": "orga_test",
            "user_id": "user_op",
            "changed_fields": (
                changed_fields if changed_fields is not None else ["status_id", "status_label"]
            ),
            "previous_data": {"status_id": _OTHER_STATUS_ID},
            "data": {"status_id": new_status_id},
            "meta": {},
            "request_id": "req_qualified_001",
        },
        "subscription_id": "whsub_qualified_test",
    }


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def stub_close_client(monkeypatch: pytest.MonkeyPatch) -> CloseClient:
    """CloseClient backed by a path-aware mock transport that returns
    the canned Lead payload for ``/api/v1/lead/`` and empty stubs for
    every other path (opportunity / activity / files). The existing
    ``_handle_lead_updated`` path runs alongside the new handler, so
    the opportunity / PDF gate reads must not 5xx."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_QUALIFIED_OPP_OPEN_STATUS_ID", _QUALIFIED_STATUS_ID)
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=_lead_payload())
        if path.startswith("/api/v1/opportunity/"):
            # No opportunity yet for a qualified-only lead. The
            # sibling lead.updated handler's gate sees this and skips
            # the auto-create branch on its own path — that's expected
            # behavior and doesn't interfere with our new handler.
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/note/") or path.startswith("/api/v1/activity/email/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/call/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        # Default: empty Lead payload so anything else 200s with no fields.
        return httpx.Response(200, json={"id": _LEAD_ID})

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture()
def client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    stub_close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
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


def _audit_actions(audit: InMemoryAuditLog, action: str) -> list[dict[str, Any]]:
    return [r for r in audit.entries if r["action"] == action]


# ----------------------------------------------------------------------
# Case (a): status change with no existing merchant → auto-create
# ----------------------------------------------------------------------


def test_qualified_status_change_creates_merchant_when_absent(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A lead.updated event whose status_id changed INTO the configured
    Qualified-Opp-Open id, when no merchant exists for the close_lead_id,
    creates a new ``MerchantRow`` populated with the Close Lead field-map
    output and writes a ``close.webhook.merchant_auto_created`` audit row."""
    assert repo.count_total() == 0

    resp = _post_signed(client, _lead_status_event())
    assert resp.status_code == 204, resp.text

    # Merchant exists in the repo, keyed by close_lead_id.
    created = repo.find_by_close_lead_id(_LEAD_ID)
    assert created is not None
    assert created.business_name == "Qualified Lead LLC"
    assert created.owner_name == "Alex Operator"
    assert created.state == "FL"
    assert created.close_lead_id == _LEAD_ID

    # Exactly one auto-created audit row, carrying the contract fields.
    auto_rows = _audit_actions(audit, "close.webhook.merchant_auto_created")
    assert len(auto_rows) == 1
    details = auto_rows[0]["details"]
    assert details["close_lead_id"] == _LEAD_ID
    assert details["merchant_id"] == str(created.id)
    # ``business_name`` is masked to ``***`` by the audit logger PII
    # filter (logger._PII_KEYS). The detail key is still PRESENT — we
    # only verify the masking applied, never the plaintext.
    assert details["business_name"] == "***"
    # At least business_name + state + owner + ein + entity_type.
    assert details["fields_populated_count"] >= 5
    assert details["trigger_status_id"] == _QUALIFIED_STATUS_ID
    assert details["event_id"] == "ev_qualified_001"

    # No skip rows fired.
    assert _audit_actions(audit, "close.webhook.qualified_skipped") == []
    assert _audit_actions(audit, "close.webhook.status_change_no_op_existing_merchant") == []


# ----------------------------------------------------------------------
# Case (b): same status change with existing merchant → no-op
# ----------------------------------------------------------------------


def test_qualified_status_change_no_op_when_merchant_exists(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """If a merchant already exists for the Close lead id (operator
    pre-created, prior redelivery, etc.) the handler writes a
    no-op audit row and creates nothing."""
    existing = repo.upsert(
        MerchantRow(
            business_name="Pre-existing Merchant",
            owner_name="J Pre",
            state="CA",
            close_lead_id=_LEAD_ID,
            status="finalized",
        )
    )
    assert repo.count_total() == 1

    resp = _post_signed(client, _lead_status_event())
    assert resp.status_code == 204, resp.text

    # Still exactly one merchant row; the pre-existing one was untouched
    # by this handler (it remains "Pre-existing Merchant" — the existing
    # sibling lead.updated upsert handler may further diff, but our
    # new handler must not create a duplicate).
    assert repo.count_total() == 1
    same = repo.find_by_close_lead_id(_LEAD_ID)
    assert same is not None
    assert same.id == existing.id

    no_op_rows = _audit_actions(audit, "close.webhook.status_change_no_op_existing_merchant")
    assert len(no_op_rows) == 1
    details = no_op_rows[0]["details"]
    assert details["close_lead_id"] == _LEAD_ID
    assert details["trigger_status_id"] == _QUALIFIED_STATUS_ID
    assert details["existing_merchant_status"] == "finalized"
    assert details["soft_deleted"] is False

    # No auto-create row fired.
    assert _audit_actions(audit, "close.webhook.merchant_auto_created") == []


# ----------------------------------------------------------------------
# Case (c): status change to a non-Qualified status → handler skips
# ----------------------------------------------------------------------


def test_status_change_to_other_status_skips_handler(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A status_id transition to anything other than the configured
    Qualified-Opp-Open id MUST NOT create a merchant. The handler
    writes a ``close.webhook.qualified_skipped`` audit row with
    reason=wrong_status."""
    other_status_id = "stat_lender_shopping_test"
    event = _lead_status_event(new_status_id=other_status_id)

    resp = _post_signed(client, event)
    assert resp.status_code == 204, resp.text

    # No merchant created by THIS handler. The sibling lead.updated
    # handler also runs but its gate (opportunity + PDF check) reads
    # the empty opportunity list our stub returns and skips too.
    assert repo.find_by_close_lead_id(_LEAD_ID) is None

    # The skip audit row pins the reason + observed status.
    skipped = _audit_actions(audit, "close.webhook.qualified_skipped")
    assert len(skipped) == 1
    details = skipped[0]["details"]
    assert details["reason"] == "wrong_status"
    assert details["observed_status_id"] == other_status_id
    assert details["trigger_status_id"] == _QUALIFIED_STATUS_ID
    assert details["close_lead_id"] == _LEAD_ID

    # No auto-create / no-op audit rows fired.
    assert _audit_actions(audit, "close.webhook.merchant_auto_created") == []
    assert _audit_actions(audit, "close.webhook.status_change_no_op_existing_merchant") == []


# ----------------------------------------------------------------------
# Case (d): lead.updated event without status_id in changed_fields → skip
# ----------------------------------------------------------------------


def test_lead_updated_without_status_id_change_skips_handler(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A lead.updated event where the operator edited fields other than
    status_id (e.g. legal_name, description) must not enter the
    auto-create branch. The handler writes a
    ``close.webhook.qualified_skipped`` audit row with
    reason=no_status_change."""
    event = _lead_status_event(changed_fields=["display_name", "description"])

    resp = _post_signed(client, event)
    assert resp.status_code == 204, resp.text

    # No merchant created by THIS handler.
    assert repo.find_by_close_lead_id(_LEAD_ID) is None

    skipped = _audit_actions(audit, "close.webhook.qualified_skipped")
    assert len(skipped) == 1
    details = skipped[0]["details"]
    assert details["reason"] == "no_status_change"
    assert details["close_lead_id"] == _LEAD_ID

    # No auto-create / no-op audit rows fired by this handler.
    assert _audit_actions(audit, "close.webhook.merchant_auto_created") == []


# ----------------------------------------------------------------------
# Bonus: non-lead events leave no qualified-skipped audit noise.
# ----------------------------------------------------------------------


def test_opportunity_updated_does_not_emit_qualified_skipped_audit(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    """Opportunity events are owned by the Pre-UW handler. The new
    Qualified-Opp-Open handler must stay silent on non-lead events
    (no extra audit noise — the receipt audit already records the
    decision)."""
    event = _lead_status_event()
    event["event"]["object_type"] = "opportunity"

    resp = _post_signed(client, event)
    assert resp.status_code == 204, resp.text

    # No skip rows from the new handler — opportunity events are
    # routed through the existing Pre-UW filter path.
    assert _audit_actions(audit, "close.webhook.qualified_skipped") == []
    assert _audit_actions(audit, "close.webhook.merchant_auto_created") == []
