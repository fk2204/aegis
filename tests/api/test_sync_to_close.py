"""POST /deals/{merchant_id}/sync-to-close route tests.

TestClient + dependency_overrides for the merchant, decision snapshot,
document repository, audit log, and Close client. The downstream
``push_decision_to_close`` is exercised end-to-end through a CloseClient
backed by ``httpx.MockTransport`` so we verify the route's pipeline
(merchant lookup → close_lead_id check → decision lookup → audit →
push) drives the right outputs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_decision_snapshot,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.close.field_map import CLOSE_FIELD_IDS
from aegis.compliance.snapshot import (
    InMemoryDecisionSnapshot,
)
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository

_BEARER = "test-token-not-real"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()


def _cf_key(aegis_name: str) -> str:
    return f"custom.{CLOSE_FIELD_IDS[aegis_name]}"


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    close_lead_id: str | None = "lead_abc",
) -> MerchantRow:
    """Drop a minimum-shape MerchantRow into the in-memory repo."""
    m = MerchantRow(
        id=uuid4(),
        business_name="Acme",
        owner_name="Jane Roe",
        state="CA",
        close_lead_id=close_lead_id,
    )
    repo.upsert(m)
    return m


def _seed_document(
    docs: InMemoryDocumentRepository,
    merchant_id: UUID,
    *,
    suffix: str = "",
) -> UUID:
    """Create a placeholder document tied to the merchant so that the
    snapshot's find_latest_for_merchant has a deal_id to match against."""
    row = docs.create_document(
        file_hash=f"sha-{merchant_id.hex[:8]}{suffix}",
        byte_size=1024,
        original_filename="stmt.pdf",
        merchant_id=merchant_id,
    )
    return row.id


def _seed_decision(
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
    *,
    deal_id: UUID,
    decision: str = "approve",
    score: Decimal | None = Decimal("78"),
    decided_at: datetime | None = None,
    decision_reason_codes: list[str] | None = None,
    ofac_cache_timestamp: datetime | None = None,
) -> UUID:
    """Append a decision row directly to the in-memory store. We bypass
    snapshot.write() so we can set decided_at deterministically — write()
    leaves it None (DB default applies)."""
    decision_id = uuid4()
    row: dict[str, Any] = {
        "id": str(decision_id),
        "deal_id": str(deal_id),
        "decided_at": (decided_at or datetime.now(UTC)).isoformat(),
        "decided_by": "api",
        "decision": decision,
        "decision_reason_codes": list(decision_reason_codes or []),
        "score": str(score) if score is not None else None,
        "score_factors": {},
        "ofac_cache_timestamp": (
            ofac_cache_timestamp.isoformat() if ofac_cache_timestamp else None
        ),
        "state_code": "CA",
        "cfdl_tier": 1,
        "aegis_version": "test",
        "rule_pack_version": "test",
    }
    snapshot._rows.append(row)
    return decision_id


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def snapshot() -> InMemoryDecisionSnapshot:
    return InMemoryDecisionSnapshot()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def close_transport_requests() -> list[httpx.Request]:
    """Recorded list — populated by the close_client transport so tests
    can introspect what was sent."""
    return []


@pytest.fixture
def close_get_response() -> dict[str, Any]:
    """Mutable canned response for the Close GET /lead/<id>/ call. Tests
    mutate this before issuing the request to drive different paths."""
    return {"id": "lead_abc", "display_name": "Acme"}


@pytest.fixture
def close_get_status() -> dict[str, int]:
    """Mutable status-code holder. Tests set this to 200/404/500 etc."""
    return {"code": 200}


@pytest.fixture
def close_put_status() -> dict[str, int]:
    return {"code": 200}


@pytest.fixture
def close_client(
    monkeypatch: pytest.MonkeyPatch,
    close_transport_requests: list[httpx.Request],
    close_get_response: dict[str, Any],
    close_get_status: dict[str, int],
    close_put_status: dict[str, int],
) -> CloseClient:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        close_transport_requests.append(request)
        if request.method == "GET":
            if close_get_status["code"] == 200:
                return httpx.Response(200, json=close_get_response)
            return httpx.Response(close_get_status["code"], text="boom")
        if request.method == "PUT":
            if close_put_status["code"] == 200:
                return httpx.Response(200, json={"id": "lead_abc"})
            return httpx.Response(close_put_status["code"], text="put-boom")
        return httpx.Response(405)

    return CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    )


@pytest.fixture
def client(
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_decision_snapshot] = lambda: snapshot
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _post(client: TestClient, merchant_id: UUID) -> Any:
    return client.post(
        f"/deals/{merchant_id}/sync-to-close",
        headers={"Authorization": f"Bearer {_BEARER}"},
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


def test_happy_path_patches_and_returns_synced(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
    close_transport_requests: list[httpx.Request],
) -> None:
    merchant = _seed_merchant(merchants)
    doc_id = _seed_document(docs, merchant.id)
    decision_id = _seed_decision(
        snapshot,
        audit,
        deal_id=doc_id,
        decision="approve",
        score=Decimal("78"),
        ofac_cache_timestamp=datetime.now(UTC),  # → Clear
    )

    resp = _post(client, merchant.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merchant_id"] == str(merchant.id)
    assert body["close_lead_id"] == "lead_abc"
    assert body["decision_id"] == str(decision_id)
    assert body["patched"] is True
    assert body["reason"] == "patched"
    # First push fills all 4 business fields.
    assert set(body["fields_diffed"]) == {
        "aegis_applicant_id",
        "aegis_score",
        "aegis_recommendation",
        "ofac_status",
    }

    # CloseClient made one GET + one PUT.
    methods = [r.method for r in close_transport_requests]
    assert methods == ["GET", "PUT"]

    # PUT carried the right Aegis values.
    put_body = json.loads(close_transport_requests[1].content)
    assert put_body[_cf_key("aegis_score")] == 78
    assert put_body[_cf_key("aegis_recommendation")] == "Approve"
    assert put_body[_cf_key("ofac_status")] == "Clear"
    assert put_body[_cf_key("aegis_applicant_id")] == str(decision_id)


def test_no_diff_path_returns_patched_false(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
    close_get_response: dict[str, Any],
    close_transport_requests: list[httpx.Request],
) -> None:
    """If Close already holds the same Aegis-* values, the downstream
    push_decision_to_close skips the PATCH and we surface
    patched=False with reason=no_diff."""
    merchant = _seed_merchant(merchants)
    doc_id = _seed_document(docs, merchant.id)
    decision_id = _seed_decision(
        snapshot,
        audit,
        deal_id=doc_id,
        decision="approve",
        score=Decimal("78"),
        ofac_cache_timestamp=datetime.now(UTC),
    )

    # Close already has matching values.
    close_get_response.update(
        {
            _cf_key("aegis_applicant_id"): str(decision_id),
            _cf_key("aegis_score"): 78,
            _cf_key("aegis_recommendation"): "Approve",
            _cf_key("ofac_status"): "Clear",
        }
    )

    resp = _post(client, merchant.id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["patched"] is False
    assert body["fields_diffed"] == []
    assert body["reason"] == "no_diff"

    # Only GET fired; no PUT.
    methods = [r.method for r in close_transport_requests]
    assert methods == ["GET"]


# ----------------------------------------------------------------------
# Error paths
# ----------------------------------------------------------------------


def test_404_when_merchant_missing(client: TestClient) -> None:
    resp = _post(client, uuid4())
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_400_when_merchant_has_no_close_lead_id(
    client: TestClient, merchants: InMemoryMerchantRepository
) -> None:
    merchant = _seed_merchant(merchants, close_lead_id=None)
    resp = _post(client, merchant.id)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "close_lead_id" in detail
    assert "/webhooks/close" in detail  # error message points operator at fix


def test_400_when_merchant_has_no_decision(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
) -> None:
    merchant = _seed_merchant(merchants)
    _seed_document(docs, merchant.id)
    # No decision seeded.

    resp = _post(client, merchant.id)
    assert resp.status_code == 400
    assert "no recorded decision" in resp.json()["detail"]


def test_502_on_close_5xx_after_retries(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
    close_get_status: dict[str, int],
) -> None:
    merchant = _seed_merchant(merchants)
    doc_id = _seed_document(docs, merchant.id)
    _seed_decision(snapshot, audit, deal_id=doc_id)
    close_get_status["code"] = 500

    resp = _post(client, merchant.id)
    assert resp.status_code == 502
    assert "close_upstream_error" in resp.json()["detail"]


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------


def test_audit_close_deal_sync_triggered_written_before_push(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
) -> None:
    """Verifies a close.deal.sync_triggered audit row lands with the
    expected fields BEFORE the downstream close.lead.sync_attempted row.
    """
    merchant = _seed_merchant(merchants)
    doc_id = _seed_document(docs, merchant.id)
    decision_id = _seed_decision(snapshot, audit, deal_id=doc_id)

    resp = _post(client, merchant.id)
    assert resp.status_code == 200

    triggers = [e for e in audit.entries if e["action"] == "close.deal.sync_triggered"]
    attempts = [
        e for e in audit.entries if e["action"] == "close.lead.sync_attempted"
    ]
    assert len(triggers) == 1
    assert len(attempts) == 1

    t = triggers[0]
    assert t["actor"] == "api"
    assert t["subject_type"] == "merchant"
    assert t["subject_id"] == str(merchant.id)
    assert t["details"]["merchant_id"] == str(merchant.id)
    assert t["details"]["decision_id"] == str(decision_id)
    assert t["details"]["close_lead_id"] == "lead_abc"


def test_audit_carries_actor_email_when_header_present(
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
) -> None:
    """Cf-Access-Authenticated-User-Email header → actor_email on audit row."""
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_decision_snapshot] = lambda: snapshot
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_close_client] = lambda: close_client

    merchant = _seed_merchant(merchants)
    doc_id = _seed_document(docs, merchant.id)
    _seed_decision(snapshot, audit, deal_id=doc_id)

    with TestClient(app) as tc:
        resp = tc.post(
            f"/deals/{merchant.id}/sync-to-close",
            headers={
                "Authorization": f"Bearer {_BEARER}",
                "Cf-Access-Authenticated-User-Email": "filip@commerafunding.com",
            },
        )
    assert resp.status_code == 200

    triggers = [e for e in audit.entries if e["action"] == "close.deal.sync_triggered"]
    assert len(triggers) == 1
    assert triggers[0]["actor_email"] == "filip@commerafunding.com"

    app.dependency_overrides.clear()
    reset_dependency_caches()


# ----------------------------------------------------------------------
# Latest decision picked across multiple documents
# ----------------------------------------------------------------------


def test_latest_decision_picked_across_merchant_documents(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    snapshot: InMemoryDecisionSnapshot,
    audit: InMemoryAuditLog,
) -> None:
    """A merchant with TWO documents and TWO decisions (one per doc)
    must surface the most-recent decision (by decided_at), not just
    whichever was inserted first."""
    merchant = _seed_merchant(merchants)
    doc_a = _seed_document(docs, merchant.id, suffix="-a")
    doc_b = _seed_document(docs, merchant.id, suffix="-b")

    # Older decision on doc_a.
    _seed_decision(
        snapshot,
        audit,
        deal_id=doc_a,
        decision="decline",
        score=Decimal("30"),
        decided_at=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
    )
    # Newer decision on doc_b — this is what should be pushed.
    newer_id = _seed_decision(
        snapshot,
        audit,
        deal_id=doc_b,
        decision="approve",
        score=Decimal("80"),
        decided_at=datetime(2026, 5, 21, 10, 0, tzinfo=UTC),
    )

    resp = _post(client, merchant.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision_id"] == str(newer_id)
