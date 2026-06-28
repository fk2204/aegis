"""Webhook-flow integration test for the migration-087 Close FINANCIAL
block parser.

Runs the full Close webhook → MerchantRow path against an inbound Lead
payload whose ``description`` field carries the structured FINANCIAL
block. Asserts the parsed values land on the merchant row through the
real ``_upsert_merchant_from_lead`` code path — NOT a unit-level
helper invocation.

The fixture description matches the verified shape observed on prod
leads (see migration 087 docstring).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
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
from aegis.merchants.repository import InMemoryMerchantRepository

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"

_LEAD_ID = "lead_fin_087"

_DESCRIPTION = """\
Inbound from broker — funded once before, wants more.

FINANCIAL:
  Requested Amount: 499999
  Use of Funds: Expansion / Build-out
  Monthly Gross Revenue: 199999
  Avg Monthly CC Sales: 200000
  Monthly Deposits: 20
  Existing MCA Positions: 2
  Current Lenders: Diesel, Finpoint
  Existing MCA Balance: 8001
  Daily/Weekly Payment: 125000
  Bank: Third Coast Bank
"""


def _sign(timestamp: str, body: bytes) -> str:
    secret = bytes.fromhex(_TEST_SECRET_HEX)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _lead_payload() -> dict[str, Any]:
    return {
        "id": _LEAD_ID,
        "display_name": "Vibration Guys Inc",
        f"custom.{CLOSE_FIELD_IDS['legal_name']}": "Vibration Guys Inc",
        # Description carries the FINANCIAL block — the migration-087
        # parser pulls everything out of here.
        "description": _DESCRIPTION,
    }


def _opportunity_event() -> dict[str, Any]:
    return {
        "event": {
            "id": "ev_fin_087",
            "date_created": "2026-06-28T10:00:00+00:00",
            "action": "updated",
            "object_type": "opportunity",
            "object_id": "oppo_fin_087",
            "lead_id": _LEAD_ID,
            "organization_id": "orga_fin",
            "user_id": "user_op",
            "changed_fields": ["status_id"],
            "previous_data": {"status_id": "stat_other"},
            "data": {"status_id": _TRIGGER_STATUS_ID},
            "meta": {},
            "request_id": "req_fin_087",
        },
        "subscription_id": "whsub_fin",
    }


def _path_aware_transport(
    lead_payload: dict[str, Any],
) -> Callable[[httpx.Request], httpx.Response]:
    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=lead_payload)
        if path.startswith("/api/v1/opportunity/"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "oppo_fin_087", "lead_id": _LEAD_ID}],
                    "has_more": False,
                },
            )
        if path.startswith("/api/v1/activity/note/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/email/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(200, json=lead_payload)

    return transport


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    lead_payload: dict[str, Any],
) -> Iterator[TestClient]:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()
    transport = _path_aware_transport(lead_payload)
    close_client = CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client
    with TestClient(app) as tc:
        yield tc
    reset_dependency_caches()


def _post(client: TestClient, body: dict[str, Any]) -> Any:
    ts = str(int(time.time()))
    raw = json.dumps(body).encode("utf-8")
    sig = _sign(ts, raw)
    return client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": sig,
            "close-sig-timestamp": ts,
        },
    )


def test_webhook_extracts_financial_block_into_merchant_row(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Full webhook → MerchantRow flow. Every FINANCIAL-block field
    must land on the merchant row through the real upsert path."""
    lead = _lead_payload()
    for client in _make_client(monkeypatch, repo=repo, audit=audit, lead_payload=lead):
        resp = _post(client, _opportunity_event())
        assert resp.status_code == 204, resp.text

    merchants = list(repo._by_id.values())
    assert len(merchants) == 1
    m = merchants[0]

    # Every stated field surfaces — Decimal precision preserved.
    assert m.requested_amount == Decimal("499999")
    assert m.use_of_funds == "Expansion / Build-out"
    assert m.monthly_revenue == Decimal("199999")
    assert m.avg_monthly_cc_sales == Decimal("200000")
    assert m.stated_monthly_deposits == 20
    assert m.stated_mca_positions == 2
    assert m.stated_current_lenders == ["Diesel", "Finpoint"]
    assert m.stated_mca_balance == Decimal("8001")
    # The hard-dep field name read by detect_impossible_payment_load.
    assert m.stated_daily_payment == Decimal("125000")
    assert m.stated_bank == "Third Coast Bank"
