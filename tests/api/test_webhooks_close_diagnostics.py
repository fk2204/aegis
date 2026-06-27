"""Tests for the diagnostic audit-row + warning-log coverage on
``POST /webhooks/close`` 400/401 reject paths.

Background: the 2026-06-27 prod incident showed a constant 400 Bad
Request flood on the webhook with ZERO application-level error logs —
the access-log INFO line was the only trace. Every reject path now
writes a ``close.webhook.hmac_fail`` or ``close.webhook.bad_request``
audit row carrying the source IP + a non-PII reason token. This file
locks in the contract: each reject MUST emit its audit row.

The HMAC-fail reason tokens:
  * secret_unconfigured     (503)
  * missing_headers          (401)
  * bad_timestamp_format     (401)
  * stale_timestamp          (401, also writes age_seconds)
  * secret_not_hex           (503)
  * signature_mismatch       (401)

The 400 reject reason tokens:
  * malformed_json
  * payload_not_dict
  * missing_event
  * lead_id_missing_on_trigger
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
from aegis.config import get_settings
from aegis.merchants.repository import InMemoryMerchantRepository

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"


def _sign(timestamp: str, body: bytes, secret_hex: str = _TEST_SECRET_HEX) -> str:
    secret = bytes.fromhex(secret_hex)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _trigger_event_no_lead_id() -> dict[str, Any]:
    """Trigger-matching event WITHOUT a lead_id — exercises the
    stage-4 ``lead_id_missing_on_trigger`` 400 path."""
    return {
        "event": {
            "id": "ev_test_diag_no_lead",
            "date_created": "2026-06-27T03:00:00+00:00",
            "action": "updated",
            "object_type": "opportunity",
            "object_id": "oppo_xyz",
            # lead_id deliberately absent
            "changed_fields": ["status_id"],
            "previous_data": {"status_id": "stat_old"},
            "data": {"status_id": _TRIGGER_STATUS_ID},
            "meta": {},
            "request_id": "req_diag",
        },
        "subscription_id": "whsub_test_diag",
    }


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def stub_close_client(monkeypatch: pytest.MonkeyPatch) -> CloseClient:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    # Minimal transport — none of the diagnostic tests reach the Close
    # API. Any path returns an empty 200 just so the client constructs.
    def transport(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

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


def _rows(audit: InMemoryAuditLog, action: str) -> list[dict[str, Any]]:
    return [e for e in audit.entries if e["action"] == action]


# ----------------------------------------------------------------------
# HMAC fails — every 401/503 path writes close.webhook.hmac_fail
# ----------------------------------------------------------------------


def test_hmac_missing_headers_writes_audit(client: TestClient, audit: InMemoryAuditLog) -> None:
    resp = client.post(
        "/webhooks/close",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401
    rows = _rows(audit, "close.webhook.hmac_fail")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "missing_headers"
    assert rows[0]["details"]["status_code"] == 401
    assert rows[0]["details"]["body_bytes"] == 2
    # client_ip will be set by Starlette TestClient ("testclient" or a 127.0.0.1)
    assert "client_ip" in rows[0]["details"]


def test_hmac_bad_timestamp_format_writes_audit(
    client: TestClient, audit: InMemoryAuditLog
) -> None:
    raw = b"{}"
    sig = _sign("not-a-timestamp", raw)
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": "not-a-timestamp",
        },
    )
    assert resp.status_code == 401
    rows = _rows(audit, "close.webhook.hmac_fail")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "bad_timestamp_format"


def test_hmac_stale_timestamp_writes_audit_with_age(
    client: TestClient, audit: InMemoryAuditLog
) -> None:
    stale_ts = str(int(time.time()) - 10 * 60)  # 10 minutes old
    raw = b"{}"
    sig = _sign(stale_ts, raw)
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": stale_ts,
        },
    )
    assert resp.status_code == 401
    rows = _rows(audit, "close.webhook.hmac_fail")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "stale_timestamp"
    assert rows[0]["details"]["age_seconds"] >= 600


def test_hmac_signature_mismatch_writes_audit(client: TestClient, audit: InMemoryAuditLog) -> None:
    raw = b"{}"
    ts = str(int(time.time()))
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": "0" * 64,  # wrong sig
            "close-sig-timestamp": ts,
        },
    )
    assert resp.status_code == 401
    rows = _rows(audit, "close.webhook.hmac_fail")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "signature_mismatch"


def test_hmac_secret_unconfigured_writes_audit(
    client: TestClient,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLOSE_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    resp = client.post(
        "/webhooks/close",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 503
    rows = _rows(audit, "close.webhook.hmac_fail")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "secret_unconfigured"
    assert rows[0]["details"]["status_code"] == 503


# ----------------------------------------------------------------------
# 400 paths — each writes close.webhook.bad_request
# ----------------------------------------------------------------------


def test_400_malformed_json_writes_audit(client: TestClient, audit: InMemoryAuditLog) -> None:
    raw = b"{not-json"
    ts = str(int(time.time()))
    sig = _sign(ts, raw)
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": ts,
        },
    )
    assert resp.status_code == 400
    rows = _rows(audit, "close.webhook.bad_request")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "malformed_json"


def test_400_payload_not_dict_writes_audit(client: TestClient, audit: InMemoryAuditLog) -> None:
    raw = b'"a bare string body"'  # valid JSON, not a dict
    ts = str(int(time.time()))
    sig = _sign(ts, raw)
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": ts,
        },
    )
    assert resp.status_code == 400
    rows = _rows(audit, "close.webhook.bad_request")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "payload_not_dict"
    assert "str" in rows[0]["details"]["detail"]


def test_400_missing_event_writes_audit(client: TestClient, audit: InMemoryAuditLog) -> None:
    raw = json.dumps({"subscription_id": "whsub_x"}).encode("utf-8")  # no 'event' key
    ts = str(int(time.time()))
    sig = _sign(ts, raw)
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": ts,
        },
    )
    assert resp.status_code == 400
    rows = _rows(audit, "close.webhook.bad_request")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "missing_event"


def test_400_lead_id_missing_on_trigger_writes_audit(
    client: TestClient, audit: InMemoryAuditLog
) -> None:
    """Trigger-matching opportunity event missing lead_id → 400 + audit."""
    raw = json.dumps(_trigger_event_no_lead_id()).encode("utf-8")
    ts = str(int(time.time()))
    sig = _sign(ts, raw)
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": ts,
        },
    )
    assert resp.status_code == 400
    rows = _rows(audit, "close.webhook.bad_request")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "lead_id_missing_on_trigger"
    # event id is the non-PII pointer we expose for cross-ref against the
    # close.webhook.received row written upstream.
    assert "ev_test_diag_no_lead" in rows[0]["details"]["detail"]
    # The receipt audit was still written before the 400 reject.
    assert len(_rows(audit, "close.webhook.received")) == 1


# ----------------------------------------------------------------------
# Audit-row PII discipline — diagnostic rows MUST NOT carry body content
# ----------------------------------------------------------------------


def test_hmac_fail_audit_does_not_carry_signature_or_body(
    client: TestClient, audit: InMemoryAuditLog
) -> None:
    """The diagnostic audit row must surface a reason + IP + byte count
    only — never the presented signature, timestamp value, or any body
    content. Locks in the PII discipline so a future edit can't quietly
    start logging the secret-bytes that arrive on the wire."""
    raw = b'{"some":"payload"}'
    ts = str(int(time.time()))
    resp = client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": "deadbeef" * 8,
            "close-sig-timestamp": ts,
        },
    )
    assert resp.status_code == 401
    rows = _rows(audit, "close.webhook.hmac_fail")
    assert len(rows) == 1
    forbidden = {"deadbeef" * 8, ts, "some", "payload"}
    serialized = json.dumps(rows[0]["details"])
    for token in forbidden:
        assert token not in serialized, f"audit row leaked {token!r}"
