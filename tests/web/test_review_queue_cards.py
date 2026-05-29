"""Tests for ``_build_review_queue_cards`` — chunk C of Proposal 4.

Per-document cards for the manual-review queue, sharing the Today card
vocabulary (categorized flags, merchant header) but one card per
document rather than grouped by merchant. Tier is deal-level so the
builder caches it per merchant — N docs from one merchant produce one
score_deal call, not N.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import DocumentRow
from aegis.web._attention_card import ReviewQueueCard
from aegis.web.router import _build_review_queue_cards


def _doc(
    *,
    merchant_id: UUID | None,
    fraud_score: int | None,
    uploaded_at: datetime,
    flags: list[str] | None = None,
    filename: str = "doc.pdf",
) -> DocumentRow:
    return DocumentRow(
        id=uuid4(),
        file_hash=uuid4().hex,
        byte_size=1024,
        original_filename=filename,
        merchant_id=merchant_id,
        parse_status="manual_review",
        fraud_score=fraud_score,
        all_flags=flags or [],
        uploaded_at=uploaded_at,
    )


class _StubDocRepo:
    """Stand-in for DocumentRepository — only ``list_transactions`` /
    ``get_analysis`` would be hit by ``_collect_analyzed_for_merchant``.
    Returning empty makes tier resolution fall back to None safely, so
    these tests focus on the per-doc shape and tier caching without
    requiring a fully wired scoring path."""

    def list_documents(self, **_: object) -> list[DocumentRow]:
        return []

    def get_analysis(self, *_: object, **__: object) -> object | None:
        return None

    def list_transactions(self, *_: object, **__: object) -> list[object]:
        return []


# ---------------------------------------------------------------------------
# Per-document shape
# ---------------------------------------------------------------------------


def test_one_card_per_document_not_grouped_by_merchant() -> None:
    """Three docs for one merchant produce three cards (not one)."""
    m = MerchantRow(business_name="Acme Inc", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=m.id, fraud_score=70, uploaded_at=base, filename="apr.pdf"),
        _doc(merchant_id=m.id, fraud_score=75, uploaded_at=base, filename="may.pdf"),
        _doc(merchant_id=m.id, fraud_score=80, uploaded_at=base, filename="jun.pdf"),
    ]
    cards = _build_review_queue_cards(docs, repo, _StubDocRepo(), None)  # type: ignore[arg-type]

    assert len(cards) == 3
    assert {c.filename for c in cards} == {"apr.pdf", "may.pdf", "jun.pdf"}
    # Every card has the same merchant context; per-doc fields differ.
    assert {c.merchant_label for c in cards} == {"Acme Inc"}


def test_per_doc_fields_populated() -> None:
    m = MerchantRow(
        business_name="Risky Ltd",
        owner_name="J",
        state="TX",
        industry_naics="453998",
        requested_amount="25000.00",
    )
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 14, 30, tzinfo=UTC)
    doc = _doc(
        merchant_id=m.id,
        fraud_score=82,
        uploaded_at=base,
        filename="statement.pdf",
    )
    cards = _build_review_queue_cards([doc], repo, _StubDocRepo(), None)  # type: ignore[arg-type]

    assert isinstance(cards[0], ReviewQueueCard)
    assert cards[0].document_id == str(doc.id)
    assert cards[0].filename == "statement.pdf"
    assert cards[0].uploaded_at == "2026-05-28 14:30"
    assert cards[0].fraud_score == 82
    # band derives from doc fraud_score, not merchant aggregate.
    assert cards[0].fraud_band == "decline"
    assert cards[0].merchant_state == "TX"
    assert cards[0].merchant_naics == "453998"
    assert str(cards[0].requested_amount) == "25000.00"


def test_fraud_band_is_per_doc_not_merchant_aggregate() -> None:
    """Two docs from the same merchant with different scores produce
    two cards with different fraud_band values."""
    m = MerchantRow(business_name="Mixed", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=m.id, fraud_score=20, uploaded_at=base),
        _doc(merchant_id=m.id, fraud_score=80, uploaded_at=base),
    ]
    cards = _build_review_queue_cards(docs, repo, _StubDocRepo(), None)  # type: ignore[arg-type]

    bands = {c.fraud_score: c.fraud_band for c in cards}
    assert bands == {20: "clear", 80: "decline"}


# ---------------------------------------------------------------------------
# Categorized flags shape
# ---------------------------------------------------------------------------


def test_flags_categorized_per_doc_not_aggregated() -> None:
    """Each card's flags come from THAT document's all_flags — never
    aggregated across the merchant's other docs in the queue."""
    m = MerchantRow(business_name="Per-doc", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    doc_a = _doc(
        merchant_id=m.id,
        fraud_score=70,
        uploaded_at=base,
        flags=["[PATTERN] mca_stacking: 1 MCA position(s) detected"],
    )
    doc_b = _doc(
        merchant_id=m.id,
        fraud_score=75,
        uploaded_at=base,
        flags=["[PATTERN] wash_deposit_suspected: 2 round-trip pairs within 5 days"],
    )
    cards = _build_review_queue_cards(
        [doc_a, doc_b], repo, _StubDocRepo(), None  # type: ignore[arg-type]
    )

    # doc_a's card holds only stacking; doc_b's card holds only the
    # decline-class wash flag. No cross-pollination.
    by_id = {c.document_id: c for c in cards}
    a = by_id[str(doc_a.id)]
    b = by_id[str(doc_b.id)]
    assert "stacking" in a.flags.by_category
    assert a.flags.decline_class == []
    assert b.flags.by_category == {}
    assert [hf.code for hf in b.flags.decline_class] == ["wash_deposit_suspected"]


# ---------------------------------------------------------------------------
# Merchant edge cases
# ---------------------------------------------------------------------------


def test_orphan_merchant_id_falls_back_to_short_label() -> None:
    """When the merchant repo doesn't know the merchant_id (deleted
    merchant or pre-onboarded doc), the card label degrades to a stub
    instead of crashing the queue."""
    repo = InMemoryMerchantRepository()
    orphan_id = UUID("11111111-2222-3333-4444-555566667777")

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [_doc(merchant_id=orphan_id, fraud_score=50, uploaded_at=base)]
    cards = _build_review_queue_cards(docs, repo, _StubDocRepo(), None)  # type: ignore[arg-type]

    assert len(cards) == 1
    assert cards[0].merchant_label.startswith("merchant 11111111")
    assert "deleted" in cards[0].merchant_label
    # Stub merchant -> tier resolution skipped -> None.
    assert cards[0].tier is None


def test_none_merchant_id_keeps_dash_label_and_no_tier() -> None:
    """Docs with merchant_id=None still render — the card just has the
    dash label and tier=None. They never crash the queue."""
    repo = InMemoryMerchantRepository()

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [_doc(merchant_id=None, fraud_score=42, uploaded_at=base)]
    cards = _build_review_queue_cards(docs, repo, _StubDocRepo(), None)  # type: ignore[arg-type]

    assert len(cards) == 1
    assert cards[0].merchant_label == "—"
    assert cards[0].merchant_id is None
    assert cards[0].tier is None


# ---------------------------------------------------------------------------
# Edge / empty
# ---------------------------------------------------------------------------


def test_empty_document_list_returns_empty_card_list() -> None:
    repo = InMemoryMerchantRepository()
    assert _build_review_queue_cards([], repo, _StubDocRepo(), None) == []  # type: ignore[arg-type]
