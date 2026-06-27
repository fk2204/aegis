"""Webhook-flow tests for the migration-080 ``product_type`` plumbing.

Covers three scenarios:

* Close webhook arrives with NO Product Type field configured →
  ``FieldMapError`` is caught and the merchant lands with the project
  default (``revenue_based``). This is the live state at migration-080
  time — the operator has not yet created the Close custom field.
* Close webhook arrives with a recognized Product Type choice → merchant
  lands with the mapped literal.
* Close webhook arrives with an UNRECOGNIZED Product Type choice →
  merchant lands with the default + a ``close.field_parse_warning``
  audit row surfaces the raw value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
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
from aegis.product_types import DEFAULT_PRODUCT_TYPE

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"
_STUB_PRODUCT_TYPE_CFID = "cf_test_product_type_stub"


def _sign(timestamp: str, body: bytes) -> str:
    secret = bytes.fromhex(_TEST_SECRET_HEX)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _base_lead_payload(close_lead_id: str = "lead_pt") -> dict[str, Any]:
    """Minimal Lead payload — only the fields needed to satisfy the
    webhook's merchant-create branch. No Product Type custom field —
    so the default applies unless the test injects one."""
    return {
        "id": close_lead_id,
        "display_name": "Product Type Test Co",
        f"custom.{CLOSE_FIELD_IDS['legal_name']}": "Product Type Test LLC",
    }


def _opportunity_event(lead_id: str = "lead_pt") -> dict[str, Any]:
    return {
        "event": {
            "id": "ev_pt_001",
            "date_created": "2026-06-28T10:00:00+00:00",
            "action": "updated",
            "object_type": "opportunity",
            "object_id": "oppo_pt",
            "lead_id": lead_id,
            "organization_id": "orga_pt",
            "user_id": "user_op",
            "changed_fields": ["status_id"],
            "previous_data": {"status_id": "stat_other"},
            "data": {"status_id": _TRIGGER_STATUS_ID},
            "meta": {},
            "request_id": "req_pt_001",
        },
        "subscription_id": "whsub_pt",
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
                json={"data": [{"id": "oppo_pt", "lead_id": "lead_pt"}], "has_more": False},
            )
        if path.startswith("/api/v1/activity/note/"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "acti_stub",
                            "_type": "Note",
                            "attachments": [
                                {
                                    "id": "att_stub",
                                    "filename": "stmt.pdf",
                                    "content_type": "application/pdf",
                                    "url": "https://app.close.com/go/file/persisted/stub",
                                }
                            ],
                        }
                    ],
                    "has_more": False,
                },
            )
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


def test_webhook_without_product_type_field_defaults_to_revenue_based(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Live migration-080 scenario: the Close custom field hasn't been
    created yet. ``get_custom_field("product_type")`` raises
    ``FieldMapError`` (unregistered AEGIS-side name); the webhook
    handler catches it and falls back to ``revenue_based``.
    """
    lead = _base_lead_payload()
    for client in _make_client(monkeypatch, repo=repo, audit=audit, lead_payload=lead):
        resp = _post(client, _opportunity_event())
        assert resp.status_code == 204, resp.text

    merchants = list(repo._by_id.values())
    assert len(merchants) == 1
    assert merchants[0].product_type == DEFAULT_PRODUCT_TYPE
    assert merchants[0].product_type == "revenue_based"


def test_webhook_with_known_product_type_lands_on_merchant(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Future-state scenario: operator has created the Close Product Type
    custom field and registered the cf_id in ``CLOSE_FIELD_IDS``. The
    webhook reads the choice via ``get_custom_field`` and the strict
    path maps the choice through ``parse_product_type_safe`` to the
    appropriate literal.
    """
    monkeypatch.setitem(CLOSE_FIELD_IDS, "product_type", _STUB_PRODUCT_TYPE_CFID)
    lead = _base_lead_payload()
    lead[f"custom.{_STUB_PRODUCT_TYPE_CFID}"] = "Term Loan"

    for client in _make_client(monkeypatch, repo=repo, audit=audit, lead_payload=lead):
        resp = _post(client, _opportunity_event())
        assert resp.status_code == 204, resp.text

    merchants = list(repo._by_id.values())
    assert len(merchants) == 1
    assert merchants[0].product_type == "business_loan"


def test_webhook_with_unknown_product_type_defaults_and_audits_warning(
    monkeypatch: pytest.MonkeyPatch,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Operator-added Close field, but the choice value is something
    ``parse_product_type_safe`` doesn't recognize. Merchant lands with
    the default; a ``close.field_parse_warning`` audit row surfaces
    the raw value so the operator can extend the mapping or fix Close.
    """
    monkeypatch.setitem(CLOSE_FIELD_IDS, "product_type", _STUB_PRODUCT_TYPE_CFID)
    lead = _base_lead_payload()
    lead[f"custom.{_STUB_PRODUCT_TYPE_CFID}"] = "Quantum Capital Stack"

    for client in _make_client(monkeypatch, repo=repo, audit=audit, lead_payload=lead):
        resp = _post(client, _opportunity_event())
        assert resp.status_code == 204, resp.text

    merchants = list(repo._by_id.values())
    assert len(merchants) == 1
    assert merchants[0].product_type == DEFAULT_PRODUCT_TYPE

    # The graceful-fallback audit row carries the raw Close value so
    # the operator can extend ``_CLOSE_PRODUCT_TYPE_SUBSTRINGS``.
    warnings = [
        entry
        for entry in audit.entries
        if entry["action"] == "close.field_parse_warning"
        and entry["details"].get("field") == "product_type"
    ]
    assert len(warnings) == 1
    assert warnings[0]["details"]["raw_value"] == "Quantum Capital Stack"
