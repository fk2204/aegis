"""GET /ui/funders/{funder_id}/submit-modal — HTMX fragment that lists
recent analyzed merchants for the operator to pick when submitting a
deal to a specific funder.

Verifies:
  * happy path renders the funder name + merchant rows
  * unknown funder → 404
  * empty state when no analyzed deals exist
  * sort order — fraud_score ascending (best AEGIS deals first)
  * limit cap — 50 even when more analyzed deals exist
  * both 'proceed' and 'review' parse statuses are listed
  * each row links to the merchant's match panel with
    ?preselect_funder=<funder_id>
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_deal_repository,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.deals.repository import InMemoryDealRepository
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from tests.test_storage import _make_pipeline_result


@pytest.fixture
def funder() -> FunderRow:
    return FunderRow(
        name="Submit-Modal Capital",
        min_monthly_revenue=Decimal("10000"),
    )


@pytest.fixture
def funder_repo(funder: FunderRow) -> InMemoryFunderRepository:
    repo = InMemoryFunderRepository()
    repo.upsert(funder)
    return repo


@pytest.fixture
def merchant_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def doc_repo() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def deal_repo(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> InMemoryDealRepository:
    return InMemoryDealRepository(merchants=merchant_repo, documents=doc_repo)


@pytest.fixture
def client(
    funder_repo: InMemoryFunderRepository,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
    deal_repo: InMemoryDealRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_funder_repository] = lambda: funder_repo
    app.dependency_overrides[get_merchant_repository] = lambda: merchant_repo
    app.dependency_overrides[get_repository] = lambda: doc_repo
    app.dependency_overrides[get_deal_repository] = lambda: deal_repo
    app.dependency_overrides[get_audit] = lambda: InMemoryAuditLog()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _seed_analyzed_merchant(
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
    *,
    business_name: str,
    state: str = "CA",
    fraud_score: int | None = None,
    parse_status: str = "proceed",
) -> MerchantRow:
    """Create a merchant + analyzed document; optionally override fraud_score."""
    merchant = MerchantRow(business_name=business_name, owner_name="Owner", state=state)
    merchant_repo.upsert(merchant)
    doc = doc_repo.create_document(
        file_hash=uuid4().hex + uuid4().hex,
        byte_size=1024,
        original_filename=f"{business_name}.pdf",
    )
    doc = doc.model_copy(update={"merchant_id": merchant.id})
    doc_repo._docs[doc.id] = doc
    doc_repo.persist_parse_result(
        doc.id, result=_make_pipeline_result(), merchant_id=merchant.id
    )
    # Patch fraud_score / parse_status on the persisted document if needed.
    if fraud_score is not None or parse_status != "proceed":
        updated = doc_repo._docs[doc.id].model_copy(
            update={
                "fraud_score": fraud_score if fraud_score is not None
                else doc_repo._docs[doc.id].fraud_score,
                "parse_status": parse_status,
            }
        )
        doc_repo._docs[doc.id] = updated
    return merchant


def test_modal_renders_for_valid_funder(
    client: TestClient,
    funder: FunderRow,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    _seed_analyzed_merchant(
        merchant_repo, doc_repo, business_name="Acme Painting LLC"
    )
    resp = client.get(f"/ui/funders/{funder.id}/submit-modal")
    assert resp.status_code == 200
    body = resp.text
    assert "Record a submission to" in body
    assert funder.name in body
    assert "Acme Painting LLC" in body
    # Each row links to the match panel WITH the preselect_funder param.
    assert f"preselect_funder={funder.id}" in body


def test_modal_404_for_unknown_funder(client: TestClient) -> None:
    resp = client.get(f"/ui/funders/{uuid4()}/submit-modal")
    assert resp.status_code == 404


def test_modal_empty_state_when_no_analyzed_deals(
    client: TestClient, funder: FunderRow
) -> None:
    resp = client.get(f"/ui/funders/{funder.id}/submit-modal")
    assert resp.status_code == 200
    assert "No analyzed merchants" in resp.text


def test_modal_sorted_by_fraud_score_ascending(
    client: TestClient,
    funder: FunderRow,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    """Best AEGIS deals (lowest fraud_score) appear first."""
    _seed_analyzed_merchant(
        merchant_repo, doc_repo, business_name="Risky Risky Inc", fraud_score=80
    )
    _seed_analyzed_merchant(
        merchant_repo, doc_repo, business_name="Clean Clean Co", fraud_score=5
    )
    _seed_analyzed_merchant(
        merchant_repo, doc_repo, business_name="Middling Middling", fraud_score=40
    )
    resp = client.get(f"/ui/funders/{funder.id}/submit-modal")
    assert resp.status_code == 200
    body = resp.text
    # Find the positions of each merchant name in the response — the
    # cleaner deal (lower fraud_score) should appear earlier.
    p_clean = body.find("Clean Clean Co")
    p_middling = body.find("Middling Middling")
    p_risky = body.find("Risky Risky Inc")
    assert 0 < p_clean < p_middling < p_risky, (
        f"sort order wrong: clean={p_clean} middling={p_middling} risky={p_risky}"
    )


def test_modal_includes_both_proceed_and_review_statuses(
    client: TestClient,
    funder: FunderRow,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    _seed_analyzed_merchant(
        merchant_repo, doc_repo, business_name="Clean Deal", parse_status="proceed"
    )
    _seed_analyzed_merchant(
        merchant_repo, doc_repo, business_name="Review Deal", parse_status="review"
    )
    resp = client.get(f"/ui/funders/{funder.id}/submit-modal")
    assert resp.status_code == 200
    body = resp.text
    assert "Clean Deal" in body
    assert "Review Deal" in body
    # Visual chip distinguishes status per row.
    assert "PROCEED" in body
    assert "REVIEW" in body


def test_modal_limit_caps_at_50(
    client: TestClient,
    funder: FunderRow,
    merchant_repo: InMemoryMerchantRepository,
    doc_repo: InMemoryDocumentRepository,
) -> None:
    """When more than 50 analyzed deals exist, only 50 render in the modal.

    The route fetches the most-recent 50 from each parse_status bucket
    (proceed + review), then sorts the union by fraud_score asc. So which
    50 win is determined by recency first, then score — this test only
    asserts the cap, not which specific rows survived (the sort behavior
    is exercised by test_modal_sorted_by_fraud_score_ascending).
    """
    for i in range(60):
        _seed_analyzed_merchant(
            merchant_repo,
            doc_repo,
            business_name=f"Merchant {i:02d}",
            fraud_score=i,
        )
    resp = client.get(f"/ui/funders/{funder.id}/submit-modal")
    assert resp.status_code == 200
    body = resp.text
    # Match exact business_name surrounded by '>' / '<' to avoid substring
    # collisions ("Merchant 05" would otherwise match "Merchant 50").
    total_rendered = sum(1 for i in range(60) if f">Merchant {i:02d}<" in body)
    assert total_rendered == 50, (
        f"expected 50 merchants rendered (cap), got {total_rendered}"
    )
