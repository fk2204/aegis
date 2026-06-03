"""``/ui/upload`` provisional-create branch tests (chunk B).

Migration 034 added a behavior fork to the dashboard upload route:

* If the form's ``merchant_id`` field is empty → create one provisional
  merchant for the batch, attach every file to it, audit
  ``merchant.provisional_created``.
* If the form's ``merchant_id`` is set → existing path, unchanged
  (auto-create NOT invoked; the form-selected merchant owns the
  uploaded docs).

These tests exercise the route end-to-end via ``TestClient``, with
in-memory repos overriding the dependency factories so we can read
back the merchant + audit + document state after the POST returns.

Scope constraints (locked by operator):
* ``POST /upload`` (bearer) unchanged — not tested here.
* ``POST /uploads/from-close`` unchanged — not tested here.
* ``POST /ui/intake`` unchanged — not tested here.
"""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO

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

_PDF_BYTES = b"%PDF-1.7\n<<fake but valid magic for upload tests>>\n%%EOF"


@pytest.fixture
def merchants_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs_repo() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    merchants_repo: InMemoryMerchantRepository,
    docs_repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> Iterator[TestClient]:
    """App wired with in-memory backends so the test can inspect
    the merchants + docs + audit state after each POST."""
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants_repo
    app.dependency_overrides[get_repository] = lambda: docs_repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_funder_repository] = lambda: InMemoryFunderRepository()
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Auto-create branch
# ---------------------------------------------------------------------------


def test_ui_upload_no_merchant_creates_provisional_and_attaches_batch(
    client: TestClient,
    merchants_repo: InMemoryMerchantRepository,
    docs_repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Operator drops 3 PDFs without picking a merchant from the form.

    Asserts:
      1. Exactly ONE provisional merchant is created for the batch.
      2. All 3 documents have that merchant's id on their row.
      3. ONE ``merchant.provisional_created`` audit row fires, with
         batch_size=3 and the original filenames in details.
    """
    assert merchants_repo.count_total() == 0

    resp = client.post(
        "/ui/upload",
        data={"merchant_id": ""},
        files=[
            ("files", ("statement_jan.pdf", BytesIO(_PDF_BYTES), "application/pdf")),
            ("files", ("statement_feb.pdf", BytesIO(_PDF_BYTES + b"\n2"), "application/pdf")),
            ("files", ("statement_mar.pdf", BytesIO(_PDF_BYTES + b"\n3"), "application/pdf")),
        ],
    )
    assert resp.status_code == 200, resp.text

    # 1. Exactly one provisional created.
    all_merchants = merchants_repo.list_all()
    assert len(all_merchants) == 1
    provisional = all_merchants[0]
    assert provisional.is_provisional is True

    # 2. All 3 documents attached to it.
    docs = list(docs_repo._docs.values())
    assert len(docs) == 3
    assert all(d.merchant_id == provisional.id for d in docs)

    # 3. Exactly one merchant.provisional_created audit row.
    rows = [
        e for e in audit.entries if e["action"] == "merchant.provisional_created"
    ]
    assert len(rows) == 1
    entry = rows[0]
    assert entry["subject_id"] == str(provisional.id)
    assert entry["details"]["batch_size"] == 3
    # File names in details are NOT in the PII mask list (filenames
    # are operator-supplied, not PII per se) — pin presence.
    assert "file_names" in entry["details"]
    assert "uploaded_by" in entry["details"]


def test_ui_upload_no_merchant_with_zero_files_skips_provisional_create(
    client: TestClient,
    merchants_repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
) -> None:
    """A POST with no files arriving (operator clicked submit on an
    empty form) returns the friendly HTML error path WITHOUT creating
    a stray empty provisional. The auto-create branch is gated on
    ``valid_files`` so this is guaranteed by construction."""
    resp = client.post("/ui/upload", data={"merchant_id": ""}, files=[])
    # FastAPI's File parameter validation may catch this earlier with
    # 422, OR our friendly-error branch returns 400 — either way, no
    # provisional should land.
    assert resp.status_code in (400, 422)
    assert merchants_repo.count_total() == 0
    assert not [
        e for e in audit.entries if e["action"] == "merchant.provisional_created"
    ]


# ---------------------------------------------------------------------------
# Existing-merchant branch (regression guard)
# ---------------------------------------------------------------------------


def test_ui_upload_with_merchant_id_does_not_create_provisional(
    client: TestClient,
    merchants_repo: InMemoryMerchantRepository,
    docs_repo: InMemoryDocumentRepository,
    audit: InMemoryAuditLog,
) -> None:
    """When the operator picks a merchant from the dropdown, the
    existing path runs unchanged: no provisional create, no
    ``merchant.provisional_created`` audit row, all files attached to
    the picked merchant.

    This is the regression guard ensuring chunk B doesn't accidentally
    fork BOTH paths.
    """
    from aegis.merchants.models import MerchantRow

    existing = MerchantRow(
        business_name="Existing Acme LLC",
        owner_name="Jane Doe",
        state="CA",
    )
    existing = merchants_repo.upsert(existing)
    assert merchants_repo.count_total() == 1

    resp = client.post(
        "/ui/upload",
        data={"merchant_id": str(existing.id)},
        files=[
            ("files", ("stmt.pdf", BytesIO(_PDF_BYTES), "application/pdf")),
            ("files", ("stmt2.pdf", BytesIO(_PDF_BYTES + b"\n2"), "application/pdf")),
        ],
    )
    assert resp.status_code == 200, resp.text

    # Still exactly ONE merchant — no provisional was created.
    assert merchants_repo.count_total() == 1
    same = merchants_repo.get(existing.id)
    assert same.status == "finalized"
    assert same.business_name == "Existing Acme LLC"

    # Documents went to the existing merchant.
    docs = list(docs_repo._docs.values())
    assert len(docs) == 2
    assert all(d.merchant_id == existing.id for d in docs)

    # NO provisional_created audit row.
    assert not [
        e for e in audit.entries if e["action"] == "merchant.provisional_created"
    ]
