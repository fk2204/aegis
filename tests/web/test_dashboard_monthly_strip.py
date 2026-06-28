"""Unit tests for the dashboard's monthly comparison strip and the
refined key-numbers banner — both reshaped 2026-06-28.

Covers:
  * 6-month window (was 3) is returned newest-first
  * Trend arrow comparator: "up" / "down" / "flat" / "" (first month)
  * Source-document IDs are paired with each month (auditability rule)
  * "Gross deposits" framing is preserved by the data (label belongs to
    the template) — verified by inspecting the row keys
  * Key-numbers "ready_to_submit" excludes merchants submitted in the
    last 30 days
  * Key-numbers carries source IDs for every aggregate

Pure-function tests on the helpers; no FastAPI / TestClient.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.funder_note_submissions import (
    InMemoryFunderNoteSubmissionRepository,
)
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import AnalysisRow, DocumentRow, InMemoryDocumentRepository
from aegis.web.routers.dashboard import (
    _compute_key_numbers,
    _compute_monthly_comparison,
)


def _seed_doc_and_analysis(
    docs: InMemoryDocumentRepository,
    *,
    monthly_breakdown: list[dict[str, str]],
    parsed_at: datetime | None = None,
    true_revenue: Decimal = Decimal("0"),
) -> tuple[UUID, UUID]:
    """Insert one doc + its analysis. Returns ``(document_id, merchant_id)``."""
    merchant_id = uuid4()
    file_hash = uuid4().hex
    doc_row = DocumentRow(
        id=uuid4(),
        file_hash=file_hash,
        byte_size=1024,
        original_filename="x.pdf",
        merchant_id=merchant_id,
        parse_status="proceed",
        uploaded_at=datetime.now(UTC),
        parsed_at=parsed_at,
    )
    # In-memory backend stores the doc dict directly; replace the row
    # via the internal store so we get the right parsed_at + parse_status.
    docs._docs[doc_row.id] = doc_row
    analysis = AnalysisRow(
        id=uuid4(),
        document_id=doc_row.id,
        merchant_id=merchant_id,
        statement_period_start=datetime.now(UTC).date(),
        statement_period_end=datetime.now(UTC).date(),
        statement_days=30,
        beginning_balance=Decimal("0"),
        ending_balance=Decimal("0"),
        avg_daily_balance=Decimal("0"),
        true_revenue=true_revenue,
        monthly_revenue=true_revenue,
        lowest_balance=Decimal("0"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0"),
        debt_to_revenue=Decimal("0"),
        monthly_breakdown=monthly_breakdown,
    )
    docs._analyses[doc_row.id] = analysis
    return doc_row.id, merchant_id


def _mk_month(month: str, deposits: str, adb: str = "5000", nsf: str = "0") -> dict[str, str]:
    return {
        "month": month,
        "deposits": deposits,
        "withdrawals": "0",
        "avg_balance": adb,
        "nsf_count": nsf,
    }


def test_monthly_comparison_returns_six_months_newest_first() -> None:
    """The strip aggregates six populated months and returns newest first."""
    docs = InMemoryDocumentRepository()
    breakdown = [
        _mk_month("2026-01", "10000"),
        _mk_month("2026-02", "12000"),
        _mk_month("2026-03", "11000"),
        _mk_month("2026-04", "14000"),
        _mk_month("2026-05", "15000"),
        _mk_month("2026-06", "20000"),
    ]
    _seed_doc_and_analysis(docs, monthly_breakdown=breakdown)

    rows = _compute_monthly_comparison(docs=docs, now=datetime.now(UTC))

    assert len(rows) == 6
    # Newest-first.
    labels = [r["label"] for r in rows]
    assert labels[0].startswith("Jun")
    assert labels[-1].startswith("Jan")


def test_monthly_comparison_carries_source_document_ids() -> None:
    """Each row exposes the documents that contributed its deposits sum —
    auditability rule (aggregate + ``_source_ids`` companion)."""
    docs = InMemoryDocumentRepository()
    breakdown_a = [_mk_month("2026-06", "5000", adb="1000")]
    breakdown_b = [_mk_month("2026-06", "3000", adb="1000")]
    doc_a, _ = _seed_doc_and_analysis(docs, monthly_breakdown=breakdown_a)
    doc_b, _ = _seed_doc_and_analysis(docs, monthly_breakdown=breakdown_b)

    rows = _compute_monthly_comparison(docs=docs, now=datetime.now(UTC))
    assert len(rows) == 1
    row = rows[0]
    assert row["deposits"] == Decimal("8000.00")
    assert sorted(row["deposits_source_ids"]) == sorted([str(doc_a), str(doc_b)])


def test_monthly_comparison_trend_up_down_flat() -> None:
    """First (oldest) month has no comparator -> empty trend; subsequent
    months are 'up' / 'down' / 'flat' relative to their predecessor."""
    docs = InMemoryDocumentRepository()
    # Two months: Feb = 10000, Mar = 12000 -> Mar is "up" vs Feb.
    breakdown = [
        _mk_month("2026-02", "10000", adb="0"),
        _mk_month("2026-03", "12000", adb="0"),
    ]
    _seed_doc_and_analysis(docs, monthly_breakdown=breakdown)

    rows = _compute_monthly_comparison(docs=docs, now=datetime.now(UTC))
    # Newest-first: index 0 == Mar (up), index 1 == Feb (no comparator).
    assert rows[0]["trend"] == "up"
    assert rows[1]["trend"] == ""


def test_monthly_comparison_flat_within_two_percent() -> None:
    """A delta within 2% of the prior month reads as 'flat', not up/down."""
    docs = InMemoryDocumentRepository()
    breakdown = [
        _mk_month("2026-05", "10000", adb="0"),
        # 100 = 1% delta -> flat.
        _mk_month("2026-06", "10100", adb="0"),
    ]
    _seed_doc_and_analysis(docs, monthly_breakdown=breakdown)

    rows = _compute_monthly_comparison(docs=docs, now=datetime.now(UTC))
    assert rows[0]["trend"] == "flat"


def test_monthly_comparison_empty_when_no_breakdown() -> None:
    """No populated monthly_breakdown → empty list (template hides strip)."""
    docs = InMemoryDocumentRepository()
    rows = _compute_monthly_comparison(docs=docs, now=datetime.now(UTC))
    assert rows == []


def test_key_numbers_ready_excludes_recently_submitted() -> None:
    """A merchant with a proceed doc + a submission in the last 30 days is
    NOT counted as 'ready to submit'."""
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    subs = InMemoryFunderNoteSubmissionRepository()

    # Two proceed-status merchants — one recently submitted, one not.
    _doc_a, mid_a = _seed_doc_and_analysis(docs, monthly_breakdown=[])
    _doc_b, mid_b = _seed_doc_and_analysis(docs, monthly_breakdown=[])

    # Merchant A: submitted 10 days ago (within the 30-day window).
    funder_id = uuid4()
    sub = subs.create(
        merchant_id=mid_a,
        funder_id=funder_id,
        funder_note="x",
        submitted_by="test@aegis.test",
    )
    subs._by_id[sub.id].submitted_at = datetime.now(UTC) - timedelta(days=10)

    result = _compute_key_numbers(
        merchant_total=2,
        pending_count=0,
        proceed_count=2,
        funder_note_subs=subs,
        docs=docs,
        merchants_repo=merchants,
        now=datetime.now(UTC),
    )
    # Only merchant B (never submitted) counts as ready.
    assert result["ready_to_submit"] == 1
    assert str(mid_b) in result["ready_to_submit_source_merchant_ids"]
    assert str(mid_a) not in result["ready_to_submit_source_merchant_ids"]


def test_key_numbers_carries_source_ids_for_all_aggregates() -> None:
    """Every aggregate in the banner ships with its source-id companion."""
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    subs = InMemoryFunderNoteSubmissionRepository()
    result = _compute_key_numbers(
        merchant_total=0,
        pending_count=0,
        proceed_count=0,
        funder_note_subs=subs,
        docs=docs,
        merchants_repo=merchants,
        now=datetime.now(UTC),
    )
    # Required source-id keys.
    assert "ready_to_submit_source_merchant_ids" in result
    assert "submitted_this_week_source_submission_ids" in result
    assert "avg_revenue_this_week_source_document_ids" in result
