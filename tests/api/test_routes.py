"""Phase 5 route tests — merchants, transactions, funders, disclosures, deals.

Use the real FastAPI app with in-memory repositories injected via
``dependency_overrides``. Disclosure rendering exercises the Tier 2
generic acknowledgment path (so the test does not depend on any Tier 1
template that would require statute audits).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.compliance import states as states_module
from aegis.compliance.states import Tier2Regulation
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.parser.models import ClassifiedTransaction
from aegis.storage import InMemoryDocumentRepository
from tests.test_storage import _make_pipeline_result

AUTH = {"Authorization": "Bearer test-token-not-real"}


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    return InMemoryFunderRepository()


@pytest.fixture
def doc_repo() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    merchant_repo: InMemoryMerchantRepository,
    funder_repo: InMemoryFunderRepository,
    doc_repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# --- merchants ---------------------------------------------------------------


def _merchant_payload(state: str = "CA") -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "business_name": "Acme Inc",
        "owner_name": "Jane Doe",
        "state": state,
    }


def test_create_and_list_merchant(client: TestClient) -> None:
    body = _merchant_payload()
    resp = client.post("/merchants", json=body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["business_name"] == "Acme Inc"

    listed = client.get("/merchants", headers=AUTH).json()
    assert len(listed) == 1


def test_create_merchant_rejects_unserved_state(client: TestClient) -> None:
    body = _merchant_payload(state="TX")
    resp = client.post("/merchants", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert "state_not_served" in resp.text


def test_get_unknown_merchant_returns_404(client: TestClient) -> None:
    resp = client.get(f"/merchants/{uuid4()}", headers=AUTH)
    assert resp.status_code == 404


# --- funders -----------------------------------------------------------------


def _funder_payload() -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "name": "Test Funder",
        "active": True,
        "min_monthly_revenue": "10000.00",
        "max_positions": 1,
    }


def test_create_and_list_funder(client: TestClient) -> None:
    resp = client.post("/funders", json=_funder_payload(), headers=AUTH)
    assert resp.status_code == 201, resp.text
    listed = client.get("/funders", headers=AUTH).json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Test Funder"


def test_delete_funder(
    client: TestClient, funder_repo: InMemoryFunderRepository
) -> None:
    body = _funder_payload()
    client.post("/funders", json=body, headers=AUTH)
    fid = body["id"]
    resp = client.delete(f"/funders/{fid}", headers=AUTH)
    assert resp.status_code == 204
    assert client.get(f"/funders/{fid}", headers=AUTH).status_code == 404


# --- transactions ------------------------------------------------------------


def _seed_document_with_transactions(
    repo: InMemoryDocumentRepository,
) -> tuple[UUID, list[ClassifiedTransaction]]:
    row = repo.create_document(
        file_hash="t" * 64, byte_size=2048, original_filename="x.pdf"
    )
    repo.persist_parse_result(row.id, result=_make_pipeline_result())
    return row.id, repo.list_transactions(row.id)


def test_list_transactions_returns_classified_rows(
    client: TestClient, doc_repo: InMemoryDocumentRepository
) -> None:
    document_id, txs = _seed_document_with_transactions(doc_repo)
    resp = client.get(f"/documents/{document_id}/transactions", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == len(txs) == 1
    assert data[0]["category"] == "deposit"


def test_list_transactions_filters_by_category(
    client: TestClient, doc_repo: InMemoryDocumentRepository
) -> None:
    document_id, _ = _seed_document_with_transactions(doc_repo)
    resp = client.get(
        f"/documents/{document_id}/transactions?category=mca_debit", headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_transactions_unknown_document_404(client: TestClient) -> None:
    resp = client.get(f"/documents/{uuid4()}/transactions", headers=AUTH)
    assert resp.status_code == 404


# --- disclosures -------------------------------------------------------------


def _score_input_dict() -> dict[str, Any]:
    return {
        "merchant_id": str(uuid4()),
        "business_name": "Acme Inc",
        "owner_name": "Jane Doe",
        "state": "CA",
        "avg_daily_balance": "5000.00",
        "true_revenue": "30000.00",
        "monthly_revenue": "30000.00",
        "lowest_balance": "1000.00",
        "num_nsf": 0,
        "days_negative": 0,
        "mca_positions": 0,
        "mca_daily_total": "0.00",
        "debt_to_revenue": "0.10",
        "statement_period_start": "2026-04-01",
        "statement_period_end": "2026-04-30",
        "statement_days": 30,
        "fraud_score": 5,
        "requested_amount": "20000.00",
        "requested_factor": "1.30",
        "requested_term_days": 120,
    }


def _score_result_dict() -> dict[str, Any]:
    return {
        "score": 70,
        "tier": "B",
        "recommendation": "approve",
        "suggested_max_advance": "20000.00",
        "recommended_factor_rate": "1.29",
        "recommended_holdback_pct": "0.12",
        "estimated_payback_days": 120,
    }


def test_disclosure_render_tier3_returns_503(client: TestClient) -> None:
    # CA is now Tier 1 per docs/compliance/01_california.md; pick a state
    # that is still Tier 3 to exercise the unaudited-state route.
    body = {
        "state": "WY",
        "deal": {**_score_input_dict(), "state": "WY"},
        "score": _score_result_dict(),
    }
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 503
    assert "state_not_audited" in resp.text


def test_disclosure_render_tier2_returns_html(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Promote Hawaii to Tier 2 for this test only — restore on teardown.
    original = states_module.STATES["HI"]
    states_module.STATES["HI"] = Tier2Regulation(
        state="HI",
        state_name="Hawaii",
        verified_date=date(2026, 5, 1),
        general_law_citation="HRS § 480-2",
        citation_url="https://example.invalid/hrs-480-2",
        notes="Hawaii UDAP applies; no MCA-specific statute as of verification date.",
    )
    try:
        body = {
            "state": "HI",
            "deal": {**_score_input_dict(), "state": "HI"},
            "score": _score_result_dict(),
        }
        resp = client.post("/disclosures/render.html", json=body, headers=AUTH)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "HRS &" in resp.text or "HRS §" in resp.text
    finally:
        states_module.STATES["HI"] = original


def test_disclosure_render_unserved_state_returns_422(client: TestClient) -> None:
    body = {
        "state": "TX",
        "deal": {**_score_input_dict(), "state": "TX"},
        "score": _score_result_dict(),
    }
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert "state_not_served" in resp.text


# --- deals -------------------------------------------------------------------


def test_score_deal_returns_score_result(client: TestClient) -> None:
    # No OFAC client passed → scorer skips OFAC.
    resp = client.post("/deals/score", json=_score_input_dict(), headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "tier" in data and "score" in data
    assert data["recommendation"] in {"approve", "decline", "refer"}


# --- auth --------------------------------------------------------------------


def test_routes_require_bearer(
    merchant_repo: InMemoryMerchantRepository,
    funder_repo: InMemoryFunderRepository,
    doc_repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        for path in ("/merchants", "/funders"):
            assert c.get(path).status_code == 401
