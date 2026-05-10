"""Combined intake (POST /ui/intake) — single-shot create-merchant + N-PDFs.

Pins:
  * Empty merchant fields → re-renders with error, doesn't create.
  * 3 PDFs in one request → 3 documents, all linked to the new merchant.
  * Operator can also POST without files (merchant-only).
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
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository

_PDF_HEAD = b"%PDF-1.4\n%fake-test-pdf\n"


def _fake_pdf(suffix: bytes) -> bytes:
    """Synth byte payload that satisfies the %PDF- magic check."""
    return _PDF_HEAD + suffix + b"\n%%EOF\n"


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def doc_repo() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


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


def test_intake_form_renders(client: TestClient) -> None:
    resp = client.get("/ui/intake")
    assert resp.status_code == 200
    assert 'enctype="multipart/form-data"' in resp.text
    assert 'action="/ui/intake"' in resp.text


def test_intake_creates_merchant_and_persists_three_pdfs(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    files = [
        ("files", ("jan.pdf", _fake_pdf(b"A"), "application/pdf")),
        ("files", ("feb.pdf", _fake_pdf(b"B"), "application/pdf")),
        ("files", ("mar.pdf", _fake_pdf(b"C"), "application/pdf")),
    ]
    resp = client.post(
        "/ui/intake",
        data={
            "business_name": "Triple Drop LLC",
            "owner_name": "Pat Doe",
            "state": "CA",
            "entity_type": "llc",
            "requested_amount": "50000",
            "requested_factor": "1.30",
            "requested_term_days": "120",
            "broker_source": "Test Broker",
            "is_renewal": "false",
        },
        files=files,
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/ui/merchants/")

    merchants = merchant_repo.list_all()
    assert len(merchants) == 1
    m = merchants[0]
    assert m.business_name == "Triple Drop LLC"
    assert m.entity_type == "llc"
    assert m.broker_source == "Test Broker"

    docs = doc_repo.list_documents(merchant_id=m.id)
    assert len(docs) == 3
    assert all(d.merchant_id == m.id for d in docs)


def test_intake_without_files_creates_merchant_only(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    resp = client.post(
        "/ui/intake",
        data={
            "business_name": "No Statements Yet LLC",
            "owner_name": "Sam Roe",
            "state": "FL",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert merchant_repo.list_all()
    assert not doc_repo.list_documents(limit=10)


def test_intake_rejects_unserved_state_without_creating(
    client: TestClient, merchant_repo: InMemoryMerchantRepository
) -> None:
    resp = client.post(
        "/ui/intake",
        data={
            "business_name": "Texas Test",
            "owner_name": "Ima Test",
            "state": "TX",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert merchant_repo.list_all() == []


def test_intake_persists_renewal_flag(
    client: TestClient, merchant_repo: InMemoryMerchantRepository
) -> None:
    resp = client.post(
        "/ui/intake",
        data={
            "business_name": "Repeat Customer",
            "owner_name": "Re Newal",
            "state": "CA",
            "is_renewal": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    [m] = merchant_repo.list_all()
    assert m.is_renewal is True


def test_dashboard_upload_no_bearer_works(
    client: TestClient,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    """The /ui/upload route is the bug fix — must accept multipart without bearer."""
    from aegis.merchants.models import MerchantRow

    m = merchant_repo.upsert(
        MerchantRow(business_name="Direct Upload LLC", owner_name="DU", state="CA")
    )
    resp = client.post(
        "/ui/upload",
        data={"merchant_id": str(m.id)},
        files=[
            ("files", ("a.pdf", _fake_pdf(b"X"), "application/pdf")),
            ("files", ("b.pdf", _fake_pdf(b"Y"), "application/pdf")),
        ],
    )
    assert resp.status_code == 200, resp.text
    docs = doc_repo.list_documents(merchant_id=m.id)
    assert len(docs) == 2
