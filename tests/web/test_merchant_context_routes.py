"""Tests for the Feature D dossier Context-panel routes:

  * ``POST /ui/merchants/{id}/deal-context``       — operator save
  * ``POST /ui/merchants/{id}/close-context/refresh`` — force refresh

Coverage:
  * deal_context persists + audit row lands with length-only details
  * deal_context empty submission clears the field
  * deal_context size cap → 400
  * deal_context route 404s on unknown merchant
  * refresh route 404s when merchant has no close_lead_id
  * refresh route 503 on Close API failure + writes a failure audit
  * refresh route success path persists context + writes success audit
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

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
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operators import CF_ACCESS_EMAIL_HEADER


def _make_close_client_returning(
    *,
    monkeypatch: pytest.MonkeyPatch,
    notes_payload: dict[str, Any] | None = None,
    calls_payload: dict[str, Any] | None = None,
    lead_payload: dict[str, Any] | None = None,
    error_on_activities: bool = False,
) -> CloseClient:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()

    notes = notes_payload or {"has_more": False, "data": []}
    calls = calls_payload or {"has_more": False, "data": []}
    lead = lead_payload or {"id": "lead_x", "description": "desc"}

    def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/api/v1/activity/note/" in path:
            if error_on_activities:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=notes)
        if "/api/v1/activity/call/" in path:
            if error_on_activities:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=calls)
        if path.startswith("/api/v1/lead/"):
            return httpx.Response(200, json=lead)
        return httpx.Response(404, json={"detail": "unhandled"})

    return CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture()
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture()
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


def _build_client(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient | None = None,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: repo
    if close_client is not None:
        app.dependency_overrides[get_close_client] = lambda: close_client
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    close_lead_id: str | None = None,
) -> MerchantRow:
    m = MerchantRow(
        business_name="Feature D LLC",
        owner_name="Owner",
        state="CA",
        close_lead_id=close_lead_id,
    )
    return repo.upsert(m)


# ---------------------------------------------------------------------------
# POST /ui/merchants/{id}/deal-context
# ---------------------------------------------------------------------------


def test_deal_context_save_persists_and_audits(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(repo)
    for client in _build_client(repo, audit):
        resp = client.post(
            f"/ui/merchants/{m.id}/deal-context",
            data={"deal_context": "Broker note: prior decline, now ready"},
            headers={CF_ACCESS_EMAIL_HEADER: "op@example.com"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/ui/merchants/{m.id}"

    updated = repo.get(m.id)
    assert updated.deal_context == "Broker note: prior decline, now ready"

    rows = [e for e in audit.entries if e["action"] == "merchant.deal_context.updated"]
    assert len(rows) == 1
    assert rows[0]["actor_email"] == "op@example.com"
    assert rows[0]["details"]["length"] == len("Broker note: prior decline, now ready")
    assert rows[0]["details"]["cleared"] is False
    # Body not in audit details (sanity check — the route only persists
    # length / cleared flag).
    assert "Broker note" not in repr(rows[0]["details"])


def test_deal_context_empty_submission_clears_field(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(repo)
    repo.set_deal_context(m.id, "prior content")
    for client in _build_client(repo, audit):
        resp = client.post(
            f"/ui/merchants/{m.id}/deal-context",
            data={"deal_context": "   "},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    updated = repo.get(m.id)
    assert updated.deal_context is None

    rows = [e for e in audit.entries if e["action"] == "merchant.deal_context.updated"]
    assert rows[-1]["details"]["cleared"] is True


def test_deal_context_size_cap_returns_400(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    m = _seed_merchant(repo)
    payload = "x" * 8001
    for client in _build_client(repo, audit):
        resp = client.post(
            f"/ui/merchants/{m.id}/deal-context",
            data={"deal_context": payload},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    # Field unchanged.
    assert repo.get(m.id).deal_context is None
    assert [e for e in audit.entries if e["action"] == "merchant.deal_context.updated"] == []


def test_deal_context_404_when_merchant_missing(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    bogus = uuid4()
    for client in _build_client(repo, audit):
        resp = client.post(
            f"/ui/merchants/{bogus}/deal-context",
            data={"deal_context": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /ui/merchants/{id}/close-context/refresh
# ---------------------------------------------------------------------------


def test_refresh_close_context_success(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _seed_merchant(repo, close_lead_id="lead_d_route")
    cclient = _make_close_client_returning(
        monkeypatch=monkeypatch,
        notes_payload={
            "has_more": False,
            "data": [
                {"id": "n1", "note": "Route test note 1"},
                {"id": "n2", "note": "Route test note 2"},
            ],
        },
        calls_payload={"has_more": False, "data": [{"id": "c1", "note": "Call A"}]},
        lead_payload={"id": "lead_d_route", "description": "Lead D description"},
    )

    for client in _build_client(repo, audit, cclient):
        resp = client.post(
            f"/ui/merchants/{m.id}/close-context/refresh",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/ui/merchants/{m.id}"

    updated = repo.get(m.id)
    assert updated.close_lead_description == "Lead D description"
    assert updated.close_notes_summary == "Route test note 1\n---\nRoute test note 2"
    assert updated.close_call_transcripts == "Call A"

    rows = [e for e in audit.entries if e["action"] == "merchant.close_context.refreshed"]
    assert len(rows) == 1
    assert rows[0]["details"]["notes_pulled"] == 2
    assert rows[0]["details"]["calls_pulled"] == 1


def test_refresh_404_when_merchant_has_no_close_lead(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _seed_merchant(repo, close_lead_id=None)
    cclient = _make_close_client_returning(monkeypatch=monkeypatch)
    for client in _build_client(repo, audit, cclient):
        resp = client.post(
            f"/ui/merchants/{m.id}/close-context/refresh",
            follow_redirects=False,
        )
        assert resp.status_code == 404


def test_refresh_503_on_close_failure_writes_audit(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-triggered refresh surfaces Close failures as 503 (distinct
    from the webhook posture which silently swallows them). A failure
    audit row lands so the operator history shows the attempt."""
    m = _seed_merchant(repo, close_lead_id="lead_d_route")
    cclient = _make_close_client_returning(monkeypatch=monkeypatch, error_on_activities=True)

    for client in _build_client(repo, audit, cclient):
        resp = client.post(
            f"/ui/merchants/{m.id}/close-context/refresh",
            headers={CF_ACCESS_EMAIL_HEADER: "op@example.com"},
            follow_redirects=False,
        )
        assert resp.status_code == 503

    failures = [e for e in audit.entries if e["action"] == "merchant.close_context.refresh_failed"]
    assert len(failures) == 1
    assert failures[0]["actor_email"] == "op@example.com"
    # close_lead_id + status_code surface in the failure audit row so the
    # operator can spot which lead the failure was on.
    assert failures[0]["details"]["close_lead_id"] == "lead_d_route"
    assert failures[0]["details"]["status_code"] == 500
