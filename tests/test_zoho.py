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
from uuid import UUID

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


def test_token_refresh_does_not_put_secrets_on_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: OAuth refresh sent client_id/client_secret/refresh_token as
    URL query params; httpx logged the full URL at INFO. Secrets must travel
    in the form body so they don't appear in logged URLs or tracebacks."""
    _set_zoho_env(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return httpx.Response(
            200,
            json={"access_token": "x", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    ZohoTokenCache().get()

    # URL must be clean — no secrets in the request line.
    assert "refresh_token=" not in captured["url"]
    assert "client_secret=" not in captured["url"]
    assert "client_id=" not in captured["url"]
    assert "secret" not in captured["url"]
    assert "rt" not in captured["url"]
    assert captured["url"].endswith("/oauth/v2/token")

    # Secrets travel in the form body.
    assert "data" in captured["kwargs"], "secrets must be in body, not query"
    body = captured["kwargs"]["data"]
    assert body["refresh_token"] == "rt"  # noqa: S105 — test stub
    assert body["client_id"] == "id"
    assert body["client_secret"] == "secret"  # noqa: S105 — test stub
    assert body["grant_type"] == "refresh_token"


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


def test_client_upload_attachment_posts_multipart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U1: ZohoClient.upload_attachment hits the Attachments endpoint with
    multipart/form-data and returns the parsed body."""
    _set_zoho_env(monkeypatch)
    _stub_token_post(monkeypatch)

    captured: dict[str, Any] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            200, json={"data": [{"code": "SUCCESS", "details": {"id": "ATT_1"}}]}
        )

    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    client = ZohoClient(http_client=http_client)
    body = client.upload_attachment(
        "Leads",
        "LEAD_99",
        filename="findings_acme.csv",
        content=b"section,key,value\nmeta,gen,now\n",
        content_type="text/csv",
    )
    assert body == {"data": [{"code": "SUCCESS", "details": {"id": "ATT_1"}}]}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/crm/v8/Leads/LEAD_99/Attachments")
    assert captured["content_type"].startswith("multipart/form-data"), (
        f"expected multipart, got: {captured['content_type']!r}"
    )
    assert b"findings_acme.csv" in captured["body"]
    assert b"section,key,value" in captured["body"]


def test_sync_attach_findings_csv_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U1: attach_findings_csv MUST NOT raise on Zoho failure — the upsert
    that just preceded it is load-bearing; the attachment is best-effort.
    """
    _set_zoho_env(monkeypatch)
    _stub_token_post(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    client = ZohoClient(http_client=http_client)
    audit = InMemoryAuditLog()
    sync = ZohoSync(
        client=client,
        merchants=InMemoryMerchantRepository(),
        audit=audit,
    )
    test_merchant_id = UUID("00000000-0000-0000-0000-000000000001")
    sync.attach_findings_csv(
        module="Leads",
        record_id="LEAD_1",
        merchant_id=test_merchant_id,
        csv_bytes=b"x,y,z\n",
        filename="x.csv",
    )
    actions = [e["action"] for e in audit.entries]
    assert "zoho.attachment.failed" in actions
    assert "zoho.attachment.uploaded" not in actions


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
        if method == "POST" and path == "/crm/v8/Deals":
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
    assert client.calls[0][:2] == ("POST", "/crm/v8/Deals")
    assert any(e["action"] == "zoho.deal.upsert" for e in audit.entries)


def test_deal_payload_serializes_decimal_fields_as_strings() -> None:
    """Regression: Decimal money/rate fields must round-trip as strings, not floats.

    CLAUDE.md rule "NEVER use float for money" — Zoho payload previously
    cast Suggested_Max_Advance / Recommended_Factor_Rate /
    Recommended_Holdback_Pct via ``float()``, which corrupts 1.30 to
    1.2999999999999998 over the wire. The fix is ``str()``; this test
    locks the contract so a future hand-edit can't re-introduce float
    coercion.
    """
    from decimal import Decimal

    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = repo.upsert(
        MerchantRow(business_name="Acme Inc", owner_name="Jane Doe", state="CA")
    )
    client = _FakeZohoClient()
    sync = ZohoSync(client=client, merchants=repo, audit=audit)  # type: ignore[arg-type]

    score = ScoreResult(
        score=70,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("20000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )
    sync.push_merchant_with_score(merchant.id, score)

    deal_post = next(
        c for c in client.calls if c[0] == "POST" and c[1] == "/crm/v8/Deals"
    )
    body = deal_post[2]
    assert body is not None
    fields = body["data"][0] if "data" in body else body
    for key in (
        "Suggested_Max_Advance",
        "Recommended_Factor_Rate",
        "Recommended_Holdback_Pct",
    ):
        assert isinstance(fields[key], str), (
            f"{key}={fields[key]!r} must be str, got {type(fields[key]).__name__}"
        )
    assert fields["Suggested_Max_Advance"] == "20000.00"
    assert fields["Recommended_Factor_Rate"] == "1.30"
    assert fields["Recommended_Holdback_Pct"] == "0.12"


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
    assert client.calls[0][:2] == ("PUT", "/crm/v8/Deals/EXISTING")


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
