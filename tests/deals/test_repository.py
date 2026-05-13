"""InMemoryDealRepository tests.

Cover list filtering, get_deal happy + missing paths, and the case
where a document survives a merchant deletion (ON DELETE SET NULL on
``documents.merchant_id`` would leave an orphan; the repo skips it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aegis.deals.models import format_deal_id, parse_deal_id
from aegis.deals.repository import InMemoryDealRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository


def _make_merchant(*, state: str = "CA", name: str = "Acme Painting LLC") -> MerchantRow:
    return MerchantRow(
        business_name=name,
        owner_name="Jane Doe",
        state=state,
    )


@pytest.fixture
def repos() -> tuple[InMemoryMerchantRepository, InMemoryDocumentRepository]:
    return InMemoryMerchantRepository(), InMemoryDocumentRepository()


def test_list_deals_empty_when_no_documents(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    assert deal_repo.list_deals() == []


def test_list_deals_includes_merchant_join(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    merchant = merchants.upsert(_make_merchant(state="CA", name="Acme LLC"))
    doc = docs.create_document(
        file_hash="a" * 64,
        byte_size=1234,
        original_filename="stmt.pdf",
        merchant_id=merchant.id,
    )

    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    deals = deal_repo.list_deals()
    assert len(deals) == 1
    deal = deals[0]
    assert deal.merchant_id == merchant.id
    assert deal.document_id == doc.id
    assert deal.business_name == "Acme LLC"
    assert deal.state == "CA"
    assert deal.parse_status == "pending"
    assert deal.deal_id == format_deal_id(merchant.id, doc.id)


def test_list_deals_skips_documents_with_no_merchant(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    """Documents on the review queue (no merchant assigned) are not deals."""
    merchants, docs = repos
    docs.create_document(
        file_hash="b" * 64,
        byte_size=2000,
        original_filename="orphan.pdf",
        merchant_id=None,
    )
    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    assert deal_repo.list_deals() == []


def test_list_deals_skips_documents_whose_merchant_is_deleted(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    """ON DELETE SET NULL leaves orphans; the deal repo filters them out."""
    merchants, docs = repos
    merchant = merchants.upsert(_make_merchant())
    doc = docs.create_document(
        file_hash="c" * 64,
        byte_size=500,
        original_filename="x.pdf",
        merchant_id=merchant.id,
    )
    # Simulate the orphan: document keeps merchant_id pointing at a row
    # that no longer exists.
    merchants.delete(merchant.id)
    # The document still references the deleted merchant id; repo skips.
    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    assert deal_repo.list_deals() == []
    assert deal_repo.get_deal(format_deal_id(merchant.id, doc.id)) is None


def test_list_deals_filters_by_state(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    ca = merchants.upsert(_make_merchant(state="CA", name="Cali Co"))
    ny = merchants.upsert(_make_merchant(state="NY", name="NY Co"))
    docs.create_document(
        file_hash="d" * 64,
        byte_size=100,
        original_filename="ca.pdf",
        merchant_id=ca.id,
    )
    docs.create_document(
        file_hash="e" * 64,
        byte_size=100,
        original_filename="ny.pdf",
        merchant_id=ny.id,
    )

    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    ca_deals = deal_repo.list_deals(state="CA")
    assert len(ca_deals) == 1
    assert ca_deals[0].state == "CA"

    # State filter is case-insensitive — match merchants.repository pattern.
    lower = deal_repo.list_deals(state="ca")
    assert len(lower) == 1
    assert lower[0].state == "CA"


def test_list_deals_filters_by_merchant_id(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    m1 = merchants.upsert(_make_merchant(name="A"))
    m2 = merchants.upsert(_make_merchant(name="B"))
    docs.create_document(
        file_hash="f" * 64, byte_size=100, original_filename="1.pdf", merchant_id=m1.id
    )
    docs.create_document(
        file_hash="g" * 64, byte_size=100, original_filename="2.pdf", merchant_id=m2.id
    )

    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    only_m1 = deal_repo.list_deals(merchant_id=m1.id)
    assert len(only_m1) == 1
    assert only_m1[0].merchant_id == m1.id


def test_get_deal_roundtrip(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    merchant = merchants.upsert(_make_merchant())
    doc = docs.create_document(
        file_hash="h" * 64,
        byte_size=100,
        original_filename="rt.pdf",
        merchant_id=merchant.id,
    )

    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    deal_id = format_deal_id(merchant.id, doc.id)
    deal = deal_repo.get_deal(deal_id)
    assert deal is not None
    assert deal.deal_id == deal_id
    assert deal_repo.parse_deal_id(deal_id) == (merchant.id, doc.id)


def test_get_deal_returns_none_for_unknown_document(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    merchant = merchants.upsert(_make_merchant())
    bogus = format_deal_id(merchant.id, uuid4())
    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    assert deal_repo.get_deal(bogus) is None


def test_get_deal_returns_none_for_malformed_id(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    assert deal_repo.get_deal("not-a-deal-id") is None


def test_get_deal_rejects_mismatched_merchant_document_pair(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    """deal_id components must refer to the same merchant the doc is bound to."""
    merchants, docs = repos
    m_a = merchants.upsert(_make_merchant(name="A"))
    m_b = merchants.upsert(_make_merchant(name="B"))
    doc = docs.create_document(
        file_hash="i" * 64,
        byte_size=100,
        original_filename="a.pdf",
        merchant_id=m_a.id,
    )
    # Build a deal_id pointing at m_b but with doc that belongs to m_a.
    wrong = format_deal_id(m_b.id, doc.id)
    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    assert deal_repo.get_deal(wrong) is None


def test_list_deals_orders_most_recent_first(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    merchant = merchants.upsert(_make_merchant())
    doc_old = docs.create_document(
        file_hash="j" * 64,
        byte_size=100,
        original_filename="old.pdf",
        merchant_id=merchant.id,
    )
    doc_new = docs.create_document(
        file_hash="k" * 64,
        byte_size=100,
        original_filename="new.pdf",
        merchant_id=merchant.id,
    )
    # InMemoryDocumentRepository stamps uploaded_at = now() on create —
    # doc_new ends up later. Force-bump if the fast-machine resolution
    # ties (Windows time.time() can repeat within the same ms).
    if doc_new.uploaded_at <= doc_old.uploaded_at:
        doc_new.uploaded_at = datetime.now(UTC)

    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    deals = deal_repo.list_deals()
    assert [d.document_id for d in deals] == [doc_new.id, doc_old.id]


def test_parse_deal_id_round_trip_through_repo(
    repos: tuple[InMemoryMerchantRepository, InMemoryDocumentRepository],
) -> None:
    merchants, docs = repos
    deal_repo = InMemoryDealRepository(merchants=merchants, documents=docs)
    m, d = uuid4(), uuid4()
    composite = format_deal_id(m, d)
    assert deal_repo.parse_deal_id(composite) == (m, d)
    # And the same thing parsed via the standalone helper agrees.
    assert parse_deal_id(composite) == (m, d)
