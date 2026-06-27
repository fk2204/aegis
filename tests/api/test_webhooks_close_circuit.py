"""Tests for the Close webhook circuit-breaker wiring + the
``/ui/webhooks/circuits`` operator surface.

Validates the integration between ``WebhookCircuit`` and the
``/webhooks/close`` route — failures arrived at via HTTPException
trip the breaker; successes reset it; an already-open breaker
short-circuits with 204 + writes a ``close.webhook.circuit_open``
audit row.

The signing + lead-payload helpers are re-imported from
``test_webhooks_close`` so the two suites stay in lock-step on the
fake HMAC secret and the fixture shape.
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
    get_webhook_circuit,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.webhook_circuit import (
    OPEN_THRESHOLD,
    InMemoryCircuitBackend,
    WebhookCircuit,
)

_TEST_SECRET_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # noqa: S105 — test stub, not a real secret
_TRIGGER_STATUS_ID = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"


def _sign(timestamp: str, body: bytes) -> str:
    secret = bytes.fromhex(_TEST_SECRET_HEX)
    return hmac.new(secret, timestamp.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _opportunity_event(
    *,
    lead_id: str = "lead_circuit_test",
    new_status_id: str = _TRIGGER_STATUS_ID,
    changed_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "event": {
            "id": "ev_circuit_001",
            "date_created": "2026-06-26T10:00:00+00:00",
            "action": "updated",
            "object_type": "opportunity",
            "object_id": "oppo_circuit",
            "lead_id": lead_id,
            "organization_id": "orga_circuit",
            "user_id": "user_op",
            "changed_fields": (
                changed_fields
                if changed_fields is not None
                else ["status_id", "status_label", "date_status_changed"]
            ),
            "previous_data": {"status_id": "stat_other"},
            "data": {"status_id": new_status_id},
            "meta": {},
            "request_id": "req_circuit_001",
        },
        "subscription_id": "whsub_circuit_001",
    }


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def circuit() -> WebhookCircuit:
    return WebhookCircuit(InMemoryCircuitBackend())


@pytest.fixture()
def stub_close_client_500(monkeypatch: pytest.MonkeyPatch) -> CloseClient:
    """A CloseClient that 500s every ``GET /lead/`` — forces the
    webhook handler to raise ``HTTPException(503)`` so failures
    propagate through the circuit-breaker wrapper.
    """
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    def transport(request: httpx.Request) -> httpx.Response:
        # Every Close API call returns 500 so the webhook trip the
        # CloseError -> HTTPException(503) failure branch.
        return httpx.Response(500, json={"error": "test_fault"})

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture()
def stub_close_client_ok(monkeypatch: pytest.MonkeyPatch) -> CloseClient:
    """A CloseClient that returns a minimal-but-valid Lead payload —
    success path drives ``record_success`` on the breaker."""
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    monkeypatch.setenv("CLOSE_WEBHOOK_SECRET", _TEST_SECRET_HEX)
    monkeypatch.setenv("CLOSE_DOCS_IN_PRE_UW_STATUS_ID", _TRIGGER_STATUS_ID)
    get_settings.cache_clear()

    lead_payload = {
        "id": "lead_circuit_test",
        "display_name": "Circuit Test Inc.",
    }

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=lead_payload)
        if path.startswith("/api/v1/opportunity/"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "oppo_x", "lead_id": "lead_circuit_test"}],
                    "has_more": False,
                },
            )
        if path.startswith("/api/v1/activity/note/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/email/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if path.startswith("/api/v1/activity/call/"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(200, json=lead_payload)

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


def _build_client(
    *,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    circuit: WebhookCircuit,
    close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client
    app.dependency_overrides[get_webhook_circuit] = lambda: circuit
    with TestClient(app) as tc:
        yield tc
    reset_dependency_caches()


@pytest.fixture()
def failing_client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    circuit: WebhookCircuit,
    stub_close_client_500: CloseClient,
) -> Iterator[TestClient]:
    yield from _build_client(
        repo=repo, audit=audit, circuit=circuit, close_client=stub_close_client_500
    )


@pytest.fixture()
def succeeding_client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    circuit: WebhookCircuit,
    stub_close_client_ok: CloseClient,
) -> Iterator[TestClient]:
    yield from _build_client(
        repo=repo, audit=audit, circuit=circuit, close_client=stub_close_client_ok
    )


def _post(client: TestClient, body: dict[str, Any]) -> Any:
    timestamp = str(int(time.time()))
    raw = json.dumps(body).encode("utf-8")
    return client.post(
        "/webhooks/close",
        content=raw,
        headers={
            "content-type": "application/json",
            "close-sig-hash": _sign(timestamp, raw),
            "close-sig-timestamp": timestamp,
        },
    )


def test_five_consecutive_failures_open_the_circuit(
    failing_client: TestClient, circuit: WebhookCircuit
) -> None:
    """Each Close API 500 surfaces as HTTPException(503) — the wrapper
    records a failure per delivery. After OPEN_THRESHOLD deliveries the
    breaker is open."""
    body = _opportunity_event(lead_id="lead_circuit_test")
    for _ in range(OPEN_THRESHOLD):
        response = _post(failing_client, body)
        assert response.status_code == 503
    assert circuit.is_open("lead_circuit_test")


def test_one_success_resets_the_counter(
    succeeding_client: TestClient, circuit: WebhookCircuit
) -> None:
    """A 204-returning delivery clears the counter regardless of prior
    failures the operator may have recorded directly (simulating the
    transition from prior-fault back to healthy)."""
    # Seed the breaker just below the threshold (4 failures).
    for _ in range(OPEN_THRESHOLD - 1):
        circuit.record_failure("lead_circuit_test")
    assert not circuit.is_open("lead_circuit_test")

    response = _post(succeeding_client, _opportunity_event(lead_id="lead_circuit_test"))
    assert response.status_code == 204
    # The success path cleared the counter — a fresh failure shouldn't
    # immediately push us back over the line.
    assert not circuit.is_open("lead_circuit_test")
    circuit.record_failure("lead_circuit_test")
    assert not circuit.is_open("lead_circuit_test")


def test_open_circuit_returns_204_without_processing(
    succeeding_client: TestClient,
    circuit: WebhookCircuit,
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """An already-open circuit short-circuits with 204 BEFORE the
    receipt audit row fires, and writes ONE
    ``close.webhook.circuit_open`` row per delivery."""
    # Open the breaker out-of-band.
    for _ in range(OPEN_THRESHOLD):
        circuit.record_failure("lead_circuit_test")
    assert circuit.is_open("lead_circuit_test")

    response = _post(succeeding_client, _opportunity_event(lead_id="lead_circuit_test"))
    assert response.status_code == 204

    actions = [e["action"] for e in audit.entries]
    assert "close.webhook.circuit_open" in actions
    assert "close.webhook.received" not in actions, (
        "open-circuit short-circuit must precede the receipt audit row"
    )
    # And no merchant was created — processing was skipped.
    assert repo.count_total() == 0


def test_reset_endpoint_clears_the_key(
    succeeding_client: TestClient,
    circuit: WebhookCircuit,
    audit: InMemoryAuditLog,
) -> None:
    """``POST /ui/webhooks/circuits/{lead_id}/reset`` resets the
    counter and writes a ``close.webhook.circuit_reset`` audit row."""
    for _ in range(OPEN_THRESHOLD):
        circuit.record_failure("lead_circuit_test")
    assert circuit.is_open("lead_circuit_test")

    response = succeeding_client.post(
        "/ui/webhooks/circuits/lead_circuit_test/reset", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/webhooks/circuits"
    assert not circuit.is_open("lead_circuit_test")

    actions = [e["action"] for e in audit.entries]
    assert "close.webhook.circuit_reset" in actions


def test_circuits_view_renders_with_zero_open(
    succeeding_client: TestClient,
) -> None:
    """The index renders the empty-state when no circuits are open."""
    response = succeeding_client.get("/ui/webhooks/circuits")
    assert response.status_code == 200
    body = response.text
    assert "webhook-circuits-page" in body
    assert "webhook-circuits-empty" in body


def test_circuits_view_lists_open_circuits(
    succeeding_client: TestClient,
    circuit: WebhookCircuit,
) -> None:
    """Open circuits surface in the table with their counts."""
    for _ in range(OPEN_THRESHOLD + 2):
        circuit.record_failure("lead_alpha")
    for _ in range(OPEN_THRESHOLD):
        circuit.record_failure("lead_beta")

    response = succeeding_client.get("/ui/webhooks/circuits")
    assert response.status_code == 200
    body = response.text
    assert "lead_alpha" in body
    assert "lead_beta" in body
    # lead_alpha has higher count so it must appear first.
    assert body.find("lead_alpha") < body.find("lead_beta")
