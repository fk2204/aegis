"""Tests for the PDF dossier route ``GET /ui/merchants/{id}/dossier.pdf``.

WeasyPrint needs Pango / Cairo / HarfBuzz native libs at render time. They
ship on the Hetzner production box (``deploy/install.sh``); local Windows
dev boxes lack them. These tests detect availability by attempting a
trivial render in a session-scoped fixture and either run the full
assertion suite (Linux / WSL2 / Hetzner) or pivot to a 503 contract test
(Windows native), so the test file is portable across both environments.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from tests.test_storage import _make_pipeline_result


@pytest.fixture(scope="session")
def weasyprint_can_render() -> bool:
    """Probe whether WeasyPrint's native libs can actually render here."""
    try:
        import weasyprint

        weasyprint.HTML(string="<p>probe</p>").write_pdf()
        return True
    except Exception:
        return False


@pytest.fixture
def merchant() -> MerchantRow:
    return MerchantRow(
        business_name="Acme Painting LLC", owner_name="Jane Doe", state="CA"
    )


@pytest.fixture
def doc_repo(merchant: MerchantRow) -> InMemoryDocumentRepository:
    repo = InMemoryDocumentRepository()
    row = repo.create_document(
        file_hash="d" * 64, byte_size=1024, original_filename="statement.pdf"
    )
    row = row.model_copy(update={"merchant_id": merchant.id})
    repo._docs[row.id] = row
    repo.persist_parse_result(
        row.id, result=_make_pipeline_result(), merchant_id=merchant.id
    )
    return repo


@pytest.fixture
def client(
    merchant: MerchantRow,
    doc_repo: InMemoryDocumentRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    merchants = InMemoryMerchantRepository()
    merchants.upsert(merchant)
    audit = InMemoryAuditLog()

    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_dossier_pdf_route_returns_pdf_when_native_libs_present(
    client: TestClient,
    merchant: MerchantRow,
    weasyprint_can_render: bool,
) -> None:
    if not weasyprint_can_render:
        pytest.skip("weasyprint native libs unavailable (Windows native dev)")

    resp = client.get(f"/ui/merchants/{merchant.id}/dossier.pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("attachment; ")
    assert resp.content.startswith(b"%PDF-"), "expected PDF magic"
    assert len(resp.content) > 5_000, (
        f"PDF suspiciously small at {len(resp.content)} bytes"
    )


def test_dossier_pdf_contains_merchant_name_and_score(
    client: TestClient,
    merchant: MerchantRow,
    weasyprint_can_render: bool,
) -> None:
    if not weasyprint_can_render:
        pytest.skip("weasyprint native libs unavailable (Windows native dev)")

    import pymupdf

    resp = client.get(f"/ui/merchants/{merchant.id}/dossier.pdf")
    assert resp.status_code == 200

    with pymupdf.open(stream=resp.content, filetype="pdf") as doc:
        text = "".join(doc.load_page(i).get_text("text") for i in range(doc.page_count))

    assert merchant.business_name in text, "merchant business name missing from PDF"
    assert "Verdict" in text, "verdict section heading missing"
    assert "Cashflow evidence" in text, "cashflow section heading missing"
    assert "Statement coverage" in text, "statement coverage section missing"


def test_dossier_pdf_returns_503_when_native_libs_missing(
    client: TestClient,
    merchant: MerchantRow,
    weasyprint_can_render: bool,
) -> None:
    if weasyprint_can_render:
        pytest.skip("native libs present — 503 path not exercisable")

    resp = client.get(f"/ui/merchants/{merchant.id}/dossier.pdf")
    assert resp.status_code == 503, resp.text
    body: dict[str, Any] = resp.json()
    assert "weasyprint" in body.get("detail", "").lower()


def test_dossier_pdf_404_for_unknown_merchant(client: TestClient) -> None:
    from uuid import uuid4

    resp = client.get(f"/ui/merchants/{uuid4()}/dossier.pdf")
    assert resp.status_code == 404
