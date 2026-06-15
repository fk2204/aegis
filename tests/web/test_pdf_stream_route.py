"""GET /ui/merchants/{merchant_id}/documents/{document_id}/pdf route tests.

Chunk-C of the PDF retention redesign — the operator-visible link from
the dossier statement-ledger row. Covers:

* Happy path: route returns 200 with ``application/pdf`` content-type
  and the exact bytes that were stored by the worker step.
* 404 on missing document.
* 404 on cross-merchant access (document belongs to a different
  merchant) — also writes a ``document.pdf_streamed_denied`` audit row
  so the misuse pattern surfaces in logs.
* 404 on missing merchant.
* 404 when the document exists but no ``pdf_store`` row was written
  (legacy / pre-chunk-B doc, or worker store-step failure path).
* Audit row written on success with byte_size + sha256_prefix; never
  the bytes themselves.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_pdf_store_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.pdf_store.repository import InMemoryPdfStoreRepository
from aegis.storage import InMemoryDocumentRepository


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def pdf_store() -> InMemoryPdfStoreRepository:
    return InMemoryPdfStoreRepository()


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    pdf_store: InMemoryPdfStoreRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_pdf_store_repository] = lambda: pdf_store
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed(
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
) -> tuple[MerchantRow, UUID]:
    """Build a finalized merchant + one document owned by that merchant."""
    merchant = MerchantRow(
        business_name="Acme Painting LLC",
        state="CA",
        status="finalized",
    )
    merchants.upsert(merchant)
    doc = docs.create_document(
        file_hash=uuid4().hex + uuid4().hex,
        byte_size=1024,
        original_filename="stmt.pdf",
        merchant_id=merchant.id,
    )
    return merchant, doc.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_pdf_route_returns_original_bytes_and_writes_audit(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    pdf_store: InMemoryPdfStoreRepository,
    audit: InMemoryAuditLog,
) -> None:
    merchant, document_id = _seed(merchants, docs)
    plaintext = b"%PDF-1.7 happy-path-bytes\n" + b"\x00" * 1024
    pdf_store.store(document_id=document_id, plaintext=plaintext)

    resp = client.get(f"/ui/merchants/{merchant.id}/documents/{document_id}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == (f'inline; filename="{document_id}.pdf"')
    assert "no-store" in resp.headers["cache-control"]
    assert resp.content == plaintext

    streamed = [e for e in audit.entries if e["action"] == "document.pdf_streamed"]
    assert len(streamed) == 1
    details = streamed[0]["details"]
    assert details["merchant_id"] == str(merchant.id)
    assert details["byte_size"] == len(plaintext)
    assert details["sha256_prefix"] == hashlib.sha256(plaintext).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 404 paths
# ---------------------------------------------------------------------------


def test_pdf_route_returns_404_for_unknown_merchant(
    client: TestClient,
    docs: InMemoryDocumentRepository,
    pdf_store: InMemoryPdfStoreRepository,
) -> None:
    """Merchant doesn't exist — route 404s before consulting docs/pdf_store."""
    bogus_merchant_id = uuid4()
    bogus_document_id = uuid4()
    resp = client.get(f"/ui/merchants/{bogus_merchant_id}/documents/{bogus_document_id}/pdf")
    assert resp.status_code == 404


def test_pdf_route_returns_404_for_unknown_document(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
) -> None:
    merchant, _ = _seed(merchants, docs)
    bogus_document_id = uuid4()
    resp = client.get(f"/ui/merchants/{merchant.id}/documents/{bogus_document_id}/pdf")
    assert resp.status_code == 404


def test_pdf_route_returns_404_on_cross_merchant_access(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
    pdf_store: InMemoryPdfStoreRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Document exists, plaintext stored, but the requested merchant
    in the path is not the document's owner. Surface 404 so the
    response gives no signal the document exists elsewhere — and audit
    the attempt so the misuse pattern surfaces in logs.
    """
    owning_merchant, document_id = _seed(merchants, docs)
    other_merchant = MerchantRow(
        business_name="Different Co.",
        state="NY",
        status="finalized",
    )
    merchants.upsert(other_merchant)
    pdf_store.store(document_id=document_id, plaintext=b"%PDF-1.7\n%data\n")

    resp = client.get(f"/ui/merchants/{other_merchant.id}/documents/{document_id}/pdf")
    assert resp.status_code == 404

    denied = [e for e in audit.entries if e["action"] == "document.pdf_streamed_denied"]
    assert len(denied) == 1
    details = denied[0]["details"]
    assert details["reason"] == "cross_merchant_access"
    assert details["requested_merchant_id"] == str(other_merchant.id)
    assert details["owning_merchant_id"] == str(owning_merchant.id)


def test_pdf_route_returns_404_when_pdf_store_row_absent(
    client: TestClient,
    merchants: InMemoryMerchantRepository,
    docs: InMemoryDocumentRepository,
) -> None:
    """Legacy / pre-chunk-B document — no row in ``pdf_store``.

    The dossier link is suppressed for parse_status in ('error',
    'pending') but a stale tab could still hit the URL directly. 404
    rather than 500 so the route response is the same as "merchant
    or document not found".
    """
    merchant, document_id = _seed(merchants, docs)
    resp = client.get(f"/ui/merchants/{merchant.id}/documents/{document_id}/pdf")
    assert resp.status_code == 404
