"""Zoho client + sync + webhook tests.

The client is exercised against a stubbed httpx transport so no live
Zoho call is made. The sync tests use the in-memory merchant repo and
a fake ``ZohoClient`` to verify the outbound payload + inbound upsert.
The webhook test uses TestClient with a forged HMAC signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.scoring.models import ScoreResult
from aegis.zoho.client import ZohoClient, ZohoTokenCache
from aegis.zoho.sync import ZohoSync, ZohoSyncError

# --- token cache -------------------------------------------------------------


def _set_zoho_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_CLIENT_ID", "id")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("ZOHO_ACCOUNTS_BASE", "https://accounts.example")
    monkeypatch.setenv("ZOHO_API_BASE", "https://api.example")
    get_settings.cache_clear()


_STUB_TOKEN = "stub-token-value"  # noqa: S105 — test stub, not a real credential


def _stub_token_post(monkeypatch: pytest.MonkeyPatch, value: str = _STUB_TOKEN) -> None:
    """Stub the module-level httpx.post call the token cache makes."""

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": value, "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)


def test_token_cache_refreshes_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_zoho_env(monkeypatch)
    _stub_token_post(monkeypatch, value="abc123")

    cache = ZohoTokenCache()
    assert cache.get() == "abc123"
    # Cached on second call (no second http call required).
    assert cache.get() == "abc123"


# --- client ------------------------------------------------------------------


def test_client_sends_oauth_header_and_returns_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_zoho_env(monkeypatch)
    _stub_token_post(monkeypatch)

    captured: dict[str, Any] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "999"}]})

    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    client = ZohoClient(http_client=http_client)
    body = client.request("GET", "/crm/v6/Deals/999")
    assert body == {"data": [{"id": "999"}]}
    assert captured["headers"]["authorization"].startswith("Zoho-oauthtoken ")
    assert captured["url"].endswith("/crm/v6/Deals/999")


def test_client_retries_on_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_zoho_env(monkeypatch)
    _stub_token_post(monkeypatch)

    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    client = ZohoClient(http_client=http_client)
    assert client.request("GET", "/crm/v6/anything") == {"ok": True}
    assert calls["n"] == 2


# --- sync --------------------------------------------------------------------


class _FakeZohoClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._next_id = "ZOHO-NEW-1"

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, json))
        if method == "POST" and path == "/crm/v6/Deals":
            return {"data": [{"code": "SUCCESS", "details": {"id": self._next_id}}]}
        return {}


def _make_score() -> ScoreResult:
    from decimal import Decimal

    return ScoreResult(
        score=70,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("20000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def test_outbound_create_records_zoho_id(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = repo.upsert(
        MerchantRow(business_name="Acme Inc", owner_name="Jane Doe", state="CA")
    )
    client = _FakeZohoClient()
    sync = ZohoSync(client=client, merchants=repo, audit=audit)  # type: ignore[arg-type]

    new_id = sync.push_merchant_with_score(merchant.id, _make_score())
    assert new_id == "ZOHO-NEW-1"
    assert repo.get(merchant.id).zoho_deal_id == "ZOHO-NEW-1"
    assert client.calls[0][:2] == ("POST", "/crm/v6/Deals")
    assert any(e["action"] == "zoho.deal.upsert" for e in audit.entries)


def test_outbound_update_uses_existing_id() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = repo.upsert(
        MerchantRow(
            business_name="Acme Inc",
            owner_name="Jane Doe",
            state="CA",
            zoho_deal_id="EXISTING",
        )
    )
    client = _FakeZohoClient()
    sync = ZohoSync(client=client, merchants=repo, audit=audit)  # type: ignore[arg-type]
    out = sync.push_merchant_with_score(merchant.id, _make_score())
    assert out == "EXISTING"
    assert client.calls[0][:2] == ("PUT", "/crm/v6/Deals/EXISTING")


def test_inbound_upserts_idempotently() -> None:
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    sync = ZohoSync(client=_FakeZohoClient(), merchants=repo, audit=audit)  # type: ignore[arg-type]
    record = {
        "id": "ZD-1",
        "Account_Name": "Beta LLC",
        "Owner_Name_AEGIS": "Bob",
        "State_AEGIS": "NY",
    }
    first = sync.apply_inbound(record)
    second = sync.apply_inbound(record)
    assert first.id == second.id
    assert first.zoho_deal_id == "ZD-1"
    assert len(repo.list_all()) == 1


def test_inbound_missing_id_raises() -> None:
    sync = ZohoSync(
        client=_FakeZohoClient(),  # type: ignore[arg-type]
        merchants=InMemoryMerchantRepository(),
        audit=InMemoryAuditLog(),
    )
    with pytest.raises(ZohoSyncError):
        sync.apply_inbound({"Account_Name": "x"})


# --- webhook -----------------------------------------------------------------


def _signed_body(
    payload: dict[str, Any], secret: bytes
) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return body, sig


def _client_with_overrides() -> tuple[TestClient, InMemoryMerchantRepository]:
    reset_dependency_caches()
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    return TestClient(app), repo


def test_webhook_rejects_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "shh")
    get_settings.cache_clear()
    client, _ = _client_with_overrides()
    with client:
        resp = client.post(
            "/webhooks/zoho",
            content=b'{"timestamp": 1000}',
            headers={"x-zoho-webhook-signature": "deadbeef"},
        )
    assert resp.status_code == 401


def test_webhook_rejects_stale_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "shh")
    get_settings.cache_clear()

    body, sig = _signed_body({"timestamp": time.time() - 3600, "id": "x"}, b"shh")
    client, _ = _client_with_overrides()
    with client:
        resp = client.post(
            "/webhooks/zoho",
            content=body,
            headers={"x-zoho-webhook-signature": sig},
        )
    assert resp.status_code == 401
    assert "stale" in resp.text


def test_webhook_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "shh")
    get_settings.cache_clear()

    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "deal": {
            "id": "ZD-42",
            "Account_Name": "Gamma Co",
            "Owner_Name_AEGIS": "Carol",
            "State_AEGIS": "FL",
        },
    }
    body, sig = _signed_body(payload, b"shh")
    client, repo = _client_with_overrides()
    with client:
        resp = client.post(
            "/webhooks/zoho",
            content=body,
            headers={"x-zoho-webhook-signature": sig},
        )
    assert resp.status_code == 204
    saved = repo.find_by_zoho_deal_id("ZD-42")
    assert saved is not None
    assert saved.business_name == "Gamma Co"
