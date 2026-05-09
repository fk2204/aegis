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
def client(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_funder_repository] = lambda: InMemoryFunderRepository()
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
