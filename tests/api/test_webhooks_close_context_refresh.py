"""Tests for the Feature D Close-context auto-refresh path through
``/webhooks/close``.

Two scenarios pinned:

  1. An ``opportunity.updated`` event that resolves to a known merchant
     (the existing Pre-UW trigger path) refreshes the three Close-derived
     context columns on the merchant row using the lead payload that
     was already fetched in stage 4.

  2. An ``activity.note.created`` event whose ``lead_id`` resolves to a
     known merchant fires the refresh — independent of the existing
     note-driven auto-status flow. ``object_type='activity.note'``
     is the spec-mandated trigger.

Both scenarios MUST keep the webhook 200-OK to Close even when the
Close-context refresh would have failed (covered by a Close-side error
test).
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

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"


def _sign(timestamp: str, body: bytes) -> str:
    secret = bytes.fromhex(_TEST_SECRET_HEX)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _lead_payload(close_lead_id: str = "lead_d_test") -> dict[str, Any]:
    return {
        "id": close_lead_id,
        "display_name": "Feature D Test Co.",
        "description": "Long-form Close lead description here",
        f"custom.{CLOSE_FIELD_IDS['legal_name']}": "Feature D Test Co.",
        f"custom.{CLOSE_FIELD_IDS['owner_name']}": "Test Owner",
        f"custom.{CLOSE_FIELD_IDS['state']}": "CA",
        f"custom.{CLOSE_FIELD_IDS['industry']}": "Restaurant / Food Service",
    }


def _opportunity_event(
    lead_id: str = "lead_d_test",
    new_status_id: str = _TRIGGER_STATUS_ID,
) -> dict[str, Any]:
    return {
        "event": {
            "id": "ev_d_001",
            "date_created": "2026-06-15T10:00:00+00:00",
            "action": "updated",
            "object_type": "opportunity",
            "object_id": "oppo_d_xyz",
            "lead_id": lead_id,
            "changed_fields": ["status_id"],
            "previous_data": {"status_id": "stat_prev"},
            "data": {"status_id": new_status_id},
        },
        "subscription_id": "whsub_d_001",
    }


def _activity_note_created_event(
    lead_id: str = "lead_d_test",
) -> dict[str, Any]:
    return {
        "event": {
            "id": "ev_note_d_001",
            "date_created": "2026-06-15T11:00:00+00:00",
            "action": "created",
            "object_type": "activity.note",
            "object_id": "acti_d_note_001",
            "lead_id": lead_id,
            "changed_fields": [],
            "data": {"note": "Just talked to broker"},
        },
        "subscription_id": "whsub_d_001",
    }


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def stub_close_client(monkeypatch: pytest.MonkeyPatch) -> CloseClient:
    """A CloseClient whose underlying httpx transport returns scripted
    payloads for get_lead / list_recent_notes / list_recent_calls."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    notes_payload = {
        "has_more": False,
        "data": [
            {"id": "acti_note_d_1", "note": "Note body 1"},
            {"id": "acti_note_d_2", "note": "Note body 2"},
        ],
    }
    calls_payload = {
        "has_more": False,
        "data": [
            {"id": "acti_call_d_1", "note": "Call body 1"},
        ],
    }

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/api/v1/activity/note/" in path:
            return httpx.Response(200, json=notes_payload)
        if "/api/v1/activity/call/" in path:
            return httpx.Response(200, json=calls_payload)
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=_lead_payload())
        return httpx.Response(404, json={"detail": "unhandled"})

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


def _post_signed(c: TestClient, body_dict: dict[str, Any]) -> Any:
    timestamp = str(int(time.time()))
    raw = json.dumps(body_dict).encode("utf-8")
    sig = _sign(timestamp, raw)
    return c.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": timestamp,
        },
    )


# ---------------------------------------------------------------------------
# Pre-UW trigger refreshes context using the already-fetched lead payload
# ---------------------------------------------------------------------------


def test_pre_uw_trigger_refreshes_close_context(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    resp = _post_signed(client, _opportunity_event())
    assert resp.status_code == 204, resp.text

    merchant = repo.find_by_close_lead_id("lead_d_test")
    assert merchant is not None
    assert merchant.close_lead_description == "Long-form Close lead description here"
    assert merchant.close_notes_summary == "Note body 1\n---\nNote body 2"
    # Transcripts wear the per-call ``[Call ... — ...s]`` header emitted
    # by ``fetch_call_transcripts_for_lead``; body preserved verbatim.
    assert merchant.close_call_transcripts is not None
    assert "Call body 1" in merchant.close_call_transcripts
    assert merchant.close_call_transcripts.startswith("[Call ")

    refreshes = [e for e in audit.entries if e["action"] == "merchant.close_context.refreshed"]
    assert len(refreshes) == 1
    d = refreshes[0]["details"]
    assert d["notes_pulled"] == 2
    assert d["calls_pulled"] == 1
    assert d["lead_description_present"] is True


# ---------------------------------------------------------------------------
# activity.note.created refreshes context when merchant resolves
# ---------------------------------------------------------------------------


def test_activity_note_created_refreshes_context_for_known_merchant(
    client: TestClient,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A pre-seeded merchant linked by close_lead_id has its context
    refreshed when an activity.note webhook arrives — independent of
    the existing note-driven funder-status flow."""
    existing = MerchantRow(
        business_name="Existing Merchant LLC",
        owner_name="Owner",
        state="CA",
        close_lead_id="lead_d_test",
    )
    repo.upsert(existing)

    resp = _post_signed(client, _activity_note_created_event())
    assert resp.status_code == 204, resp.text

    refreshes = [e for e in audit.entries if e["action"] == "merchant.close_context.refreshed"]
    assert len(refreshes) == 1
    assert refreshes[0]["subject_id"] == str(existing.id)

    after = repo.get(existing.id)
    assert after.close_notes_summary == "Note body 1\n---\nNote body 2"
    # See ``test_pre_uw_trigger_refreshes_close_context`` for format detail.
    assert after.close_call_transcripts is not None
    assert "Call body 1" in after.close_call_transcripts
    assert after.close_call_transcripts.startswith("[Call ")


def test_activity_note_created_skips_when_lead_unknown(
    client: TestClient,
    audit: InMemoryAuditLog,
) -> None:
    """Note event for a lead with no AEGIS merchant: webhook still 204s
    and no context-refresh audit row is written."""
    resp = _post_signed(client, _activity_note_created_event(lead_id="lead_unknown"))
    assert resp.status_code == 204
    refreshes = [e for e in audit.entries if e["action"] == "merchant.close_context.refreshed"]
    assert refreshes == []


# ---------------------------------------------------------------------------
# Close-side failure does NOT 5xx the webhook
# ---------------------------------------------------------------------------


def test_close_context_refresh_failure_does_not_5xx_webhook(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """The webhook MUST stay 204 for Close even when the activity list
    endpoint returns a 5xx. The orchestrator's exception surfaces as
    an audit row, not a webhook 500."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/api/v1/activity/note/" in path or "/api/v1/activity/call/" in path:
            return httpx.Response(500, json={"error": "boom"})
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=_lead_payload())
        return httpx.Response(404, json={"detail": "unhandled"})

    failing_client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))

    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: failing_client
    with TestClient(app) as c:
        resp = _post_signed(c, _opportunity_event())

    # Webhook 204 — Close gets a clean ack regardless of the side-path
    # failure. Audit row records the failure for the operator.
    assert resp.status_code == 204, resp.text
    failures = [e for e in audit.entries if e["action"] == "merchant.close_context.refresh_failed"]
    assert len(failures) == 1
    reset_dependency_caches()
