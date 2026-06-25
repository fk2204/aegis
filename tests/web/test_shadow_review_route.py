"""Tests for ``GET /ui/shadow-review`` + the Today attention card.

Surface contract:
  * ``GET /ui/shadow-review`` returns 200 with the rendering listing of
    every document parsed in the last 7 days that has at least one
    ``[SHADOW] *`` flag.
  * Empty state renders when no documents qualify.
  * Today dashboard renders the new "Shadow signals this week"
    attention section (test_id ``today-attn-shadow-review``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import DocumentRow, InMemoryDocumentRepository


@pytest.fixture
def repos_and_client() -> Iterator[
    tuple[TestClient, InMemoryDocumentRepository, InMemoryMerchantRepository]
]:
    reset_dependency_caches()
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: docs
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    client = TestClient(app)
    try:
        yield client, docs, merchants
    finally:
        app.dependency_overrides.clear()
        reset_dependency_caches()


def _seed_merchant(
    merchants: InMemoryMerchantRepository,
    *,
    business_name: str = "Test Merchant LLC",
) -> MerchantRow:
    row = MerchantRow(business_name=business_name, state="CA")
    merchants._by_id[row.id] = row
    return row


def _seed_doc_with_flags(
    docs: InMemoryDocumentRepository,
    *,
    merchant_id: UUID,
    filename: str,
    all_flags: list[str],
    parsed_offset_days: int = 0,
) -> DocumentRow:
    parsed_at = datetime.now(UTC) + timedelta(days=parsed_offset_days)
    row = DocumentRow(
        id=uuid4(),
        file_hash=uuid4().hex,
        byte_size=1024,
        original_filename=filename,
        merchant_id=merchant_id,
        uploaded_by="test",
        uploaded_at=parsed_at,
        parsed_at=parsed_at,
        all_flags=list(all_flags),
    )
    docs._docs[row.id] = row
    return row


def test_shadow_review_route_renders_listing(
    repos_and_client: tuple[TestClient, InMemoryDocumentRepository, InMemoryMerchantRepository],
) -> None:
    client, docs, merchants = repos_and_client
    m = _seed_merchant(merchants, business_name="Acme Co")
    _seed_doc_with_flags(
        docs,
        merchant_id=m.id,
        filename="april-2026.pdf",
        all_flags=[
            "[SHADOW] ai_generated_statement: score=72/100",
            "[SHADOW] unreconciled_internal_transfer_v2: leg-1",
        ],
    )

    resp = client.get("/ui/shadow-review")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'data-test-id="shadow-review-page"' in body
    assert 'data-test-id="shadow-review-table"' in body
    assert "april-2026.pdf" in body
    assert "ai_generated_statement" in body
    assert "unreconciled_internal_transfer_v2" in body
    assert "Acme Co" in body


def test_shadow_review_route_empty_state(
    repos_and_client: tuple[TestClient, InMemoryDocumentRepository, InMemoryMerchantRepository],
) -> None:
    client, _docs, _merchants = repos_and_client
    resp = client.get("/ui/shadow-review")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-test-id="shadow-review-empty"' in body
    assert "No shadow flags" in body


def test_shadow_review_route_excludes_docs_outside_window(
    repos_and_client: tuple[TestClient, InMemoryDocumentRepository, InMemoryMerchantRepository],
) -> None:
    client, docs, merchants = repos_and_client
    m = _seed_merchant(merchants)
    # Old enough to be outside the 7-day window.
    _seed_doc_with_flags(
        docs,
        merchant_id=m.id,
        filename="stale.pdf",
        all_flags=["[SHADOW] ai_generated_statement: hit"],
        parsed_offset_days=-14,
    )
    resp = client.get("/ui/shadow-review")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-test-id="shadow-review-empty"' in body
    assert "stale.pdf" not in body


def test_today_dashboard_renders_shadow_review_section(
    repos_and_client: tuple[TestClient, InMemoryDocumentRepository, InMemoryMerchantRepository],
) -> None:
    """Today index renders the new ``today-attn-shadow-review`` section."""
    client, docs, merchants = repos_and_client
    m = _seed_merchant(merchants, business_name="Shadow-fire Co")
    _seed_doc_with_flags(
        docs,
        merchant_id=m.id,
        filename="recent.pdf",
        all_flags=["[SHADOW] ai_generated_statement: hit"],
    )
    resp = client.get("/ui/")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'data-test-id="today-attn-shadow-review"' in body
    assert "Shadow signals this week" in body
    # The seeded merchant should surface as a card link.
    assert "Shadow-fire Co" in body


def test_today_dashboard_renders_empty_shadow_section_when_no_fires(
    repos_and_client: tuple[TestClient, InMemoryDocumentRepository, InMemoryMerchantRepository],
) -> None:
    client, _docs, _merchants = repos_and_client
    resp = client.get("/ui/")
    assert resp.status_code == 200
    body = resp.text
    assert 'data-test-id="today-attn-shadow-review"' in body
    assert "No shadow flags fired in the last 7 days." in body
