"""Tests for the multi-document trend narrative.

`_compute_trend` in api/routes/findings.py picks the latest vs prior
analyzed document (by statement_period_end) and computes revenue, NSF,
and ADB deltas. Returns None when fewer than 2 analyzed documents
exist — the merchant detail page hides the section in that case.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.api.routes.findings import _compute_trend
from aegis.storage import (
    AnalysisRow,
    DocumentRow,
    InMemoryDocumentRepository,
)


def _doc(uploaded_at: datetime) -> DocumentRow:
    return DocumentRow(
        id=uuid4(),
        file_hash="h" * 64,
        byte_size=1024,
        original_filename="x.pdf",
        parse_status="proceed",
        fraud_score=10,
        uploaded_at=uploaded_at,
    )


def _analysis(
    *,
    document_id: UUID,
    period_start: date,
    period_end: date,
    true_revenue: str,
    avg_daily_balance: str,
    num_nsf: int,
) -> AnalysisRow:
    return AnalysisRow(
        id=uuid4(),
        document_id=document_id,
        statement_period_start=period_start,
        statement_period_end=period_end,
        statement_days=(period_end - period_start).days,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("1000.00"),
        avg_daily_balance=Decimal(avg_daily_balance),
        true_revenue=Decimal(true_revenue),
        monthly_revenue=Decimal(true_revenue),
        lowest_balance=Decimal("0.00"),
        num_nsf=num_nsf,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
    )


def test_trend_none_with_single_document() -> None:
    repo = InMemoryDocumentRepository()
    doc = _doc(datetime(2026, 4, 1))
    repo._docs[doc.id] = doc
    repo._analyses[doc.id] = _analysis(
        document_id=doc.id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        true_revenue="80000.00",
        avg_daily_balance="10000.00",
        num_nsf=2,
    )
    assert _compute_trend([doc], repo) is None


def test_trend_computed_with_two_documents() -> None:
    repo = InMemoryDocumentRepository()
    apr_doc = _doc(datetime(2026, 5, 1))
    mar_doc = _doc(datetime(2026, 4, 1))
    repo._docs[apr_doc.id] = apr_doc
    repo._docs[mar_doc.id] = mar_doc
    # Latest by period_end (April) vs prior (March).
    repo._analyses[apr_doc.id] = _analysis(
        document_id=apr_doc.id,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        true_revenue="90000.00",  # +12.5% vs March's 80000
        avg_daily_balance="12000.00",  # +20% vs March's 10000
        num_nsf=4,  # +2 vs March
    )
    repo._analyses[mar_doc.id] = _analysis(
        document_id=mar_doc.id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        true_revenue="80000.00",
        avg_daily_balance="10000.00",
        num_nsf=2,
    )
    trend = _compute_trend([apr_doc, mar_doc], repo)
    assert trend is not None
    assert trend.statement_count == 2
    # 12.5% — Python uses banker's rounding so this rounds to 12.
    assert trend.revenue_delta_pct == 12
    assert trend.nsf_delta == 2
    assert trend.adb_delta_pct == 20


def test_trend_orders_by_period_end_not_upload_order() -> None:
    """Uploads in random order: trend should still pick by period_end."""
    repo = InMemoryDocumentRepository()
    # apr_doc uploaded FIRST but is the latest by period_end.
    apr_doc = _doc(datetime(2026, 3, 1))
    mar_doc = _doc(datetime(2026, 5, 1))
    repo._docs[apr_doc.id] = apr_doc
    repo._docs[mar_doc.id] = mar_doc
    repo._analyses[apr_doc.id] = _analysis(
        document_id=apr_doc.id,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        true_revenue="90000.00",
        avg_daily_balance="12000.00",
        num_nsf=4,
    )
    repo._analyses[mar_doc.id] = _analysis(
        document_id=mar_doc.id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        true_revenue="80000.00",
        avg_daily_balance="10000.00",
        num_nsf=2,
    )
    # Pass docs in upload order (mar first → apr second).
    trend = _compute_trend([mar_doc, apr_doc], repo)
    assert trend is not None
    # April is latest (period_end 04-30 > 03-31).
    assert trend.revenue_latest == Decimal("90000.00")
    assert trend.revenue_prior == Decimal("80000.00")


def test_trend_handles_zero_prior_revenue() -> None:
    """Division-by-zero guard: when prior revenue is 0, delta_pct is None."""
    repo = InMemoryDocumentRepository()
    apr_doc = _doc(datetime(2026, 5, 1))
    mar_doc = _doc(datetime(2026, 4, 1))
    repo._docs[apr_doc.id] = apr_doc
    repo._docs[mar_doc.id] = mar_doc
    repo._analyses[apr_doc.id] = _analysis(
        document_id=apr_doc.id,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        true_revenue="90000.00",
        avg_daily_balance="12000.00",
        num_nsf=4,
    )
    repo._analyses[mar_doc.id] = _analysis(
        document_id=mar_doc.id,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        true_revenue="0.00",
        avg_daily_balance="0.00",
        num_nsf=0,
    )
    trend = _compute_trend([apr_doc, mar_doc], repo)
    assert trend is not None
    assert trend.revenue_delta_pct is None
    assert trend.adb_delta_pct is None
    assert trend.nsf_delta == 4  # delta still works (no division)
