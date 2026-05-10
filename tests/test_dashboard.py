"""HTMX dashboard tests.

Verify each page renders, the merchant detail shows aggregates, and the
drill-down HTMX partial returns the contributing transactions only.
"""

from __future__ import annotations

from collections.abc import Iterator

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
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from tests.test_storage import _make_pipeline_result


@pytest.fixture
def merchant() -> MerchantRow:
    return MerchantRow(business_name="Acme Inc", owner_name="Jane Doe", state="CA")


@pytest.fixture
def merchant_repo(merchant: MerchantRow) -> InMemoryMerchantRepository:
    repo = InMemoryMerchantRepository()
    repo.upsert(merchant)
    return repo


@pytest.fixture
def doc_repo(merchant: MerchantRow) -> InMemoryDocumentRepository:
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="z" * 64, byte_size=1024, original_filename="x.pdf"
    )
    # Tie the document to the merchant + persist a parsed result.
    row = row.model_copy(update={"merchant_id": merchant.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(row.id, result=_make_pipeline_result(), merchant_id=merchant.id)
    return repo


@pytest.fixture
def funder_repo_seeded() -> InMemoryFunderRepository:
    """Funder repo populated with two test funders for /ui/funders coverage."""
    from decimal import Decimal

    from aegis.funders.models import FunderRow

    repo = InMemoryFunderRepository()
    repo.upsert(
        FunderRow(
            name="Test Capital",
            min_monthly_revenue=Decimal("25000"),
            min_credit_score=600,
            accepts_stacking=False,
        )
    )
    repo.upsert(
        FunderRow(
            name="Detail Capital",
            min_monthly_revenue=Decimal("50000"),
            excluded_states=("TX",),
            notes="Operator-curated note.",
        )
    )
    return repo


@pytest.fixture
def funder_repo() -> InMemoryFunderRepository:
    """Empty funder repo. Tests that need seeded funders use ``funder_repo_seeded``."""
    return InMemoryFunderRepository()


@pytest.fixture
def client(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
    funder_repo: InMemoryFunderRepository,
    request: pytest.FixtureRequest,
) -> Iterator[TestClient]:
    """Default client uses an empty funder repo. Tests requesting
    ``funder_repo_seeded`` get the seeded one routed in via the
    dependency override below.
    """
    if "funder_repo_seeded" in request.fixturenames:
        funder_repo = request.getfixturevalue("funder_repo_seeded")
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: InMemoryAuditLog()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_dashboard_index_renders(client: TestClient) -> None:
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert "AEGIS" in resp.text


def test_dashboard_upload_page_has_form(client: TestClient) -> None:
    resp = client.get("/ui/upload")
    assert resp.status_code == 200
    assert 'enctype="multipart/form-data"' in resp.text
    assert 'action="/upload"' in resp.text


def test_dashboard_lists_merchants(client: TestClient, merchant: MerchantRow) -> None:
    resp = client.get("/ui/merchants")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text


def test_merchant_detail_shows_aggregate_tiles(
    client: TestClient, merchant: MerchantRow
) -> None:
    resp = client.get(f"/ui/merchants/{merchant.id}")
    assert resp.status_code == 200
    assert "True Revenue" in resp.text
    assert "drill down" in resp.text


def test_aggregate_drilldown_returns_contributing_transactions(
    client: TestClient,
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    docs = list(doc_repo._docs.values())
    assert docs, "fixture should have created a document"
    document_id = docs[0].id

    resp = client.get(f"/ui/documents/{document_id}/aggregate/true_revenue")
    assert resp.status_code == 200
    # Partial header includes the aggregate label.
    assert "True Revenue" in resp.text
    # Includes the page/line refs from the synthetic transaction.
    assert "page 1" in resp.text and "line 10" in resp.text


def test_aggregate_drilldown_unknown_aggregate_400(
    client: TestClient, doc_repo: InMemoryDocumentRepository
) -> None:
    document_id = next(iter(doc_repo._docs.values())).id
    resp = client.get(f"/ui/documents/{document_id}/aggregate/not_real")
    assert resp.status_code == 400


def test_dashboard_deals_lists_merchant_with_latest_doc(
    client: TestClient, merchant: MerchantRow
) -> None:
    resp = client.get("/ui/deals")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    # The fixture's parsed result has parse_status="proceed".
    assert "tag-proceed" in resp.text


def test_dashboard_review_queue_empty_by_default(client: TestClient) -> None:
    """Fixture document is parse_status='proceed', so review queue is empty."""
    resp = client.get("/ui/review")
    assert resp.status_code == 200
    assert "No documents in manual-review state" in resp.text


def test_dashboard_review_queue_lists_manual_review_doc(
    client: TestClient,
    doc_repo: InMemoryDocumentRepository,
    merchant: MerchantRow,
) -> None:
    """Force a document into manual_review state and verify the queue surfaces it."""
    docs = list(doc_repo._docs.values())
    target = docs[0]
    flagged = target.model_copy(
        update={
            "parse_status": "manual_review",
            "fraud_score": 78,
            "all_flags": ["[META] incremental_saves: 2 EOF markers"],
        }
    )
    doc_repo._docs[target.id] = flagged

    resp = client.get("/ui/review")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    assert "incremental_saves" in resp.text
    assert "78" in resp.text


def test_dashboard_nav_links_visible(client: TestClient) -> None:
    """Phase 7A added Deals + Review + Funders to the nav."""
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert 'href="/ui/deals"' in resp.text
    assert 'href="/ui/review"' in resp.text
    assert 'href="/ui/funders"' in resp.text


def test_merchant_new_form_renders(client: TestClient) -> None:
    resp = client.get("/ui/merchants/new")
    assert resp.status_code == 200
    assert "New Merchant" in resp.text
    assert 'name="business_name"' in resp.text
    assert 'name="state"' in resp.text


def test_merchant_new_submit_creates_and_redirects(
    client: TestClient, merchant_repo: InMemoryMerchantRepository
) -> None:
    resp = client.post(
        "/ui/merchants/new",
        data={
            "business_name": "Beta Bakery LLC",
            "owner_name": "Sam Roe",
            "state": "FL",
            "credit_score": "720",
            "time_in_business_months": "36",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ui/merchants/")
    rows = merchant_repo.list_all()
    assert any(m.business_name == "Beta Bakery LLC" and m.state == "FL" for m in rows)


def test_merchant_new_submit_rejects_unserved_state(client: TestClient) -> None:
    resp = client.post(
        "/ui/merchants/new",
        data={
            "business_name": "Texas Test Co",
            "owner_name": "Ima Test",
            "state": "TX",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "TX" in resp.text or "not served" in resp.text.lower()


def test_merchant_edit_form_pre_fills(
    client: TestClient, merchant: MerchantRow
) -> None:
    resp = client.get(f"/ui/merchants/{merchant.id}/edit")
    assert resp.status_code == 200
    assert merchant.business_name in resp.text
    assert "Edit Merchant" in resp.text


def test_merchant_edit_submit_updates(
    client: TestClient,
    merchant: MerchantRow,
    merchant_repo: InMemoryMerchantRepository,
) -> None:
    resp = client.post(
        f"/ui/merchants/{merchant.id}/edit",
        data={
            "business_name": merchant.business_name,
            "owner_name": "Updated Owner",
            "state": merchant.state,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert merchant_repo.get(merchant.id).owner_name == "Updated Owner"


def test_funders_page_empty_renders(client: TestClient) -> None:
    resp = client.get("/ui/funders")
    assert resp.status_code == 200
    assert "Funders" in resp.text


def test_funders_page_shows_active_funders(
    client: TestClient, funder_repo_seeded: InMemoryFunderRepository
) -> None:
    """Inject a funder and verify it surfaces in the table."""
    resp = client.get("/ui/funders")
    assert resp.status_code == 200
    assert "Test Capital" in resp.text
    assert "$25,000" in resp.text
    assert "600" in resp.text


def test_funder_detail_renders_full_row(
    client: TestClient, funder_repo_seeded: InMemoryFunderRepository
) -> None:
    detail_funder = next(
        f for f in funder_repo_seeded.list_active() if f.name == "Detail Capital"
    )
    resp = client.get(f"/ui/funders/{detail_funder.id}")
    assert resp.status_code == 200
    assert "Detail Capital" in resp.text
    assert "Operator-curated note." in resp.text
    assert "TX" in resp.text


def test_funder_detail_404_when_missing(client: TestClient) -> None:
    from uuid import uuid4

    resp = client.get(f"/ui/funders/{uuid4()}")
    assert resp.status_code == 404
