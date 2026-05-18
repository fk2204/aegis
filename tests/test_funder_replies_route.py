"""HTTP-route tests for funder-reply ingestion (mp Phase 10).

Two entry paths share one persistence layer:

  - POST /funder-replies         (operator-paste, bearer-protected)
  - POST /funder-replies/webhook (HMAC + timestamp freshness)

The persistence + idempotency rules are exercised in ``test_funder_replies.py``;
this file covers HTTP semantics: auth, HMAC verification, freshness,
JSON shape, 503 on missing config.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Generator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_reply_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.config import get_settings
from aegis.funders.replies import InMemoryFunderReplyRepository

AUTH = {"Authorization": "Bearer test-token-not-real"}


@pytest.fixture
def reply_repo() -> InMemoryFunderReplyRepository:
    return InMemoryFunderReplyRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> Generator[str, None, None]:
    secret = "test-webhook-secret-not-real"  # noqa: S105 - test fixture
    monkeypatch.setenv("FUNDER_REPLY_WEBHOOK_SECRET", secret)
    get_settings.cache_clear()
    yield secret
    get_settings.cache_clear()


@pytest.fixture
def client(
    reply_repo: InMemoryFunderReplyRepository,
    audit: InMemoryAuditLog,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_funder_reply_repository] = lambda: reply_repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _operator_paste_body(*, status: str = "approved") -> dict[str, object]:
    return {
        "deal_id": str(uuid4()),
        "funder_id": str(uuid4()),
        "status": status,
        "raw_text": "Funder approves at 1.32 factor, $20,000 advance, payback $26,400.",
        "terms": {"amount": "20000.00", "factor": "1.32", "payback": "26400.00"},
        "parsed_confidence": 85,
    }


# ---------------------------------------------------------------------------
# Operator-paste endpoint
# ---------------------------------------------------------------------------


def test_operator_paste_requires_bearer(client: TestClient) -> None:
    resp = client.post("/funder-replies", json=_operator_paste_body())
    assert resp.status_code == 401


def test_operator_paste_persists_reply_and_returns_201(
    client: TestClient, reply_repo: InMemoryFunderReplyRepository
) -> None:
    resp = client.post(
        "/funder-replies", json=_operator_paste_body(), headers=AUTH
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "reply_id" in body
    assert body["validation_passed"] is True
    assert body["failures"] == []
    assert len(reply_repo.replies()) == 1
    persisted = reply_repo.replies()[0]
    assert persisted["status"] == "approved"
    assert persisted["ingested_via"] == "operator_paste"


def test_operator_paste_400_on_bad_status(client: TestClient) -> None:
    body = _operator_paste_body()
    body["status"] = "yolo"
    resp = client.post("/funder-replies", json=body, headers=AUTH)
    assert resp.status_code == 422  # FastAPI's Pydantic validation


def test_operator_paste_persists_even_on_math_failure(
    client: TestClient, reply_repo: InMemoryFunderReplyRepository
) -> None:
    """Approved reply with broken math → 201 (row persists at zero
    confidence so the operator can hand-correct). The validator
    failures land in the response body."""
    body = _operator_paste_body()
    body["terms"] = {
        "amount": "20000.00",
        "factor": "1.32",
        "payback": "9999.00",  # wrong
    }
    resp = client.post("/funder-replies", json=body, headers=AUTH)
    assert resp.status_code == 201
    assert resp.json()["validation_passed"] is False
    assert any(
        "amount_factor_payback_mismatch" in f for f in resp.json()["failures"]
    )
    persisted = reply_repo.replies()[0]
    assert persisted["parsed_confidence"] == 0  # lowered on validation failure


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


def _signed(body: dict[str, object], secret: str) -> tuple[bytes, str]:
    raw = json.dumps(body).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, sig


def test_webhook_returns_503_when_secret_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``FUNDER_REPLY_WEBHOOK_SECRET`` the webhook 503s rather
    than silently accepting unsigned payloads."""
    monkeypatch.delenv("FUNDER_REPLY_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    body = {**_operator_paste_body(), "timestamp": datetime.now(UTC).isoformat()}
    resp = client.post(
        "/funder-replies/webhook",
        json=body,
        headers={"x-funder-webhook-signature": "anything"},
    )
    get_settings.cache_clear()
    assert resp.status_code == 503


def test_webhook_rejects_bad_signature(
    client: TestClient, webhook_secret: str
) -> None:
    _ = webhook_secret
    body = {**_operator_paste_body(), "timestamp": datetime.now(UTC).isoformat()}
    resp = client.post(
        "/funder-replies/webhook",
        json=body,
        headers={"x-funder-webhook-signature": "deadbeef-not-a-real-hmac"},
    )
    assert resp.status_code == 401
    assert "bad webhook signature" in resp.text


def test_webhook_rejects_stale_timestamp(
    client: TestClient, webhook_secret: str
) -> None:
    body = {
        **_operator_paste_body(),
        "timestamp": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }
    raw, sig = _signed(body, webhook_secret)
    resp = client.post(
        "/funder-replies/webhook",
        content=raw,
        headers={
            "x-funder-webhook-signature": sig,
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "stale" in resp.text.lower()


def test_webhook_rejects_missing_timestamp(
    client: TestClient, webhook_secret: str
) -> None:
    body = _operator_paste_body()  # no `timestamp` field
    raw, sig = _signed(body, webhook_secret)
    resp = client.post(
        "/funder-replies/webhook",
        content=raw,
        headers={
            "x-funder-webhook-signature": sig,
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401
    assert "timestamp" in resp.text


def test_webhook_happy_path_persists_reply(
    client: TestClient,
    webhook_secret: str,
    reply_repo: InMemoryFunderReplyRepository,
) -> None:
    body = {**_operator_paste_body(), "timestamp": datetime.now(UTC).isoformat()}
    raw, sig = _signed(body, webhook_secret)
    resp = client.post(
        "/funder-replies/webhook",
        content=raw,
        headers={
            "x-funder-webhook-signature": sig,
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 201, resp.text
    persisted = reply_repo.replies()
    assert len(persisted) == 1
    assert persisted[0]["ingested_via"] == "webhook"
