"""Unit tests for ``_build_attention_groups`` — the Today page's group-by-merchant
helper that replaces the per-document queue with merchant-scoped cards.

Pure-function tests on the helper, no FastAPI / TestClient — locks down the
grouping contract independent of the template.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import DocumentRow
from aegis.web.router import _build_attention_groups


def _doc(
    *,
    merchant_id: UUID | None,
    fraud_score: int | None,
    uploaded_at: datetime,
    flags: list[str] | None = None,
) -> DocumentRow:
    """Build a DocumentRow stub. Hash uniqueness is irrelevant for these tests."""
    return DocumentRow(
        id=uuid4(),
        file_hash=uuid4().hex,
        byte_size=1024,
        original_filename="x.pdf",
        merchant_id=merchant_id,
        parse_status="manual_review",
        fraud_score=fraud_score,
        all_flags=flags or [],
        uploaded_at=uploaded_at,
    )


def test_groups_same_merchant_into_one_entry() -> None:
    """Three docs for one merchant collapse to one group with doc_count=3."""
    m = MerchantRow(business_name="Acme Inc", owner_name="Jane", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=m.id, fraud_score=20, uploaded_at=base),
        _doc(merchant_id=m.id, fraud_score=50, uploaded_at=base - timedelta(minutes=5)),
        _doc(merchant_id=m.id, fraud_score=80, uploaded_at=base - timedelta(minutes=10)),
    ]
    groups = _build_attention_groups(docs, repo)

    assert len(groups) == 1
    assert groups[0]["merchant_label"] == "Acme Inc"
    assert groups[0]["doc_count"] == 3
    assert len(groups[0]["documents"]) == 3


def test_worst_fraud_score_is_max_across_group() -> None:
    """The card-level score must reflect the worst doc, not the most recent."""
    m = MerchantRow(business_name="Risky Ltd", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=m.id, fraud_score=30, uploaded_at=base),
        _doc(merchant_id=m.id, fraud_score=88, uploaded_at=base - timedelta(minutes=5)),
        _doc(merchant_id=m.id, fraud_score=45, uploaded_at=base - timedelta(minutes=10)),
    ]
    groups = _build_attention_groups(docs, repo)

    assert groups[0]["worst_fraud_score"] == 88


def test_worst_fraud_score_is_none_when_all_docs_unscored() -> None:
    """If every doc has fraud_score=None, worst is None (not 0, not a crash)."""
    m = MerchantRow(business_name="Unscored", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=m.id, fraud_score=None, uploaded_at=base),
        _doc(merchant_id=m.id, fraud_score=None, uploaded_at=base - timedelta(minutes=1)),
    ]
    groups = _build_attention_groups(docs, repo)

    assert groups[0]["worst_fraud_score"] is None


def test_unique_flags_deduplicated_first_seen_order() -> None:
    """Flags repeated across docs in a group dedupe; first-seen order preserved."""
    m = MerchantRow(business_name="Flaggy", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(
            merchant_id=m.id,
            fraud_score=40,
            uploaded_at=base,
            flags=["[META] alpha", "[META] beta"],
        ),
        _doc(
            merchant_id=m.id,
            fraud_score=60,
            uploaded_at=base - timedelta(minutes=5),
            flags=["[META] beta", "[META] gamma"],
        ),
    ]
    groups = _build_attention_groups(docs, repo)

    assert groups[0]["unique_flags"] == ["[META] alpha", "[META] beta", "[META] gamma"]


def test_preserves_input_ordering_across_groups() -> None:
    """Group order follows the first time each merchant is seen in the input.

    list_documents returns most-recent first, so the group containing the
    most recent doc lands at the top of the queue.
    """
    a = MerchantRow(business_name="First Seen", owner_name="J", state="CA")
    b = MerchantRow(business_name="Second Seen", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(a)
    repo.upsert(b)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=a.id, fraud_score=40, uploaded_at=base),
        _doc(merchant_id=b.id, fraud_score=90, uploaded_at=base - timedelta(minutes=5)),
        _doc(merchant_id=a.id, fraud_score=50, uploaded_at=base - timedelta(minutes=10)),
    ]
    groups = _build_attention_groups(docs, repo)

    assert [g["merchant_label"] for g in groups] == ["First Seen", "Second Seen"]


def test_documents_without_merchant_id_bucket_under_dash() -> None:
    """Docs with merchant_id=None group into a single "—" card rather than
    being scattered as multiple unlabeled groups."""
    repo = InMemoryMerchantRepository()

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=None, fraud_score=30, uploaded_at=base),
        _doc(merchant_id=None, fraud_score=70, uploaded_at=base - timedelta(minutes=5)),
    ]
    groups = _build_attention_groups(docs, repo)

    assert len(groups) == 1
    assert groups[0]["merchant_label"] == "—"
    assert groups[0]["merchant_id"] is None
    assert groups[0]["doc_count"] == 2


def test_max_groups_caps_distinct_merchants() -> None:
    """max_groups truncates by distinct merchant count, not document count."""
    repo = InMemoryMerchantRepository()
    merchants = []
    for i in range(5):
        m = MerchantRow(business_name=f"M{i}", owner_name="J", state="CA")
        repo.upsert(m)
        merchants.append(m)

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [
        _doc(merchant_id=m.id, fraud_score=50, uploaded_at=base - timedelta(minutes=i))
        for i, m in enumerate(merchants)
    ]
    groups = _build_attention_groups(docs, repo, max_groups=3)

    assert len(groups) == 3
    # The first 3 merchants seen are kept; later ones dropped.
    assert [g["merchant_label"] for g in groups] == ["M0", "M1", "M2"]


def test_unknown_merchant_id_falls_back_to_short_label() -> None:
    """If the merchant lookup raises MerchantNotFoundError (orphaned doc),
    the group label falls back to "merchant <8-hex-prefix>" so the row
    is still visible rather than crashing or being dropped."""
    repo = InMemoryMerchantRepository()
    orphan_id = UUID("12345678-90ab-cdef-1234-567890abcdef")

    base = datetime(2026, 5, 28, 10, 0, tzinfo=UTC)
    docs = [_doc(merchant_id=orphan_id, fraud_score=50, uploaded_at=base)]
    groups = _build_attention_groups(docs, repo)

    assert len(groups) == 1
    assert groups[0]["merchant_label"] == "merchant 12345678"


def test_per_document_fields_present_in_output() -> None:
    """Each document in the group surfaces document_id (str), fraud_score,
    uploaded_at (formatted), and flags — what the template needs to render
    the per-doc sub-rows + review link."""
    m = MerchantRow(business_name="Acme", owner_name="J", state="CA")
    repo = InMemoryMerchantRepository()
    repo.upsert(m)

    base = datetime(2026, 5, 28, 10, 30, tzinfo=UTC)
    doc = _doc(merchant_id=m.id, fraud_score=72, uploaded_at=base, flags=["[META] foo"])
    groups = _build_attention_groups([doc], repo)

    out_doc = groups[0]["documents"][0]
    assert out_doc["document_id"] == str(doc.id)
    assert out_doc["fraud_score"] == 72
    assert out_doc["uploaded_at"] == "2026-05-28 10:30"
    assert out_doc["flags"] == ["[META] foo"]
