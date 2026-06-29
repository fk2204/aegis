"""Per-merchant trailing 6-month deposits/ADB/NSF strip on the dossier.

BUG-18 Phase 2 — the dossier renders a horizontal strip directly under
§ 1 Verdict showing the merchant's last six calendar months of gross
deposits, ADB, and NSF count, with trend arrows comparing each cell to
its predecessor.

Covered:
  * Helper unions ``analysis.monthly_breakdown`` across proceed
    documents, returning at most 6 cells in calendar ASC order with
    correct trend arrows.
  * Helper returns an empty list when no proceed document carries a
    populated breakdown, and the template hides the strip entirely.
  * Helper dedupes overlapping months between adjacent statements by
    keeping the entry from the freshest document.
  * Template renders the strip with the dashboard's ``.monthly-strip``
    + ``.month-cell`` classes and dossier-scoped data-test-ids.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from aegis.merchants.models import MerchantRow
from aegis.storage import AnalysisRow, DocumentRow, ParseStatus
from aegis.web._templates import templates
from aegis.web.routers.merchants import _build_merchant_monthly_trend

# ---------------------------------------------------------------------------
# Helper construction utilities
# ---------------------------------------------------------------------------


def _make_doc(
    *,
    parse_status: str = "proceed",
    uploaded_at: datetime,
    doc_id: UUID | None = None,
) -> DocumentRow:
    """Minimal DocumentRow shaped for the merchant-trend helper.

    Only ``parse_status`` + ``uploaded_at`` + ``id`` drive the helper's
    behavior; everything else carries defensible defaults so the row
    validates without forcing test callers to thread irrelevant
    plumbing.
    """
    return DocumentRow(
        id=doc_id or uuid4(),
        file_hash=uuid4().hex + uuid4().hex,
        byte_size=1024,
        original_filename="stmt.pdf",
        parse_status=cast("ParseStatus", parse_status),
        uploaded_at=uploaded_at,
    )


def _make_analysis(
    *,
    document_id: UUID,
    months: list[dict[str, str]],
) -> AnalysisRow:
    """Minimal AnalysisRow carrying a custom ``monthly_breakdown``.

    Period / balance fields are filled with neutral values so the
    Pydantic validators pass; the helper only reads ``monthly_breakdown``.
    """
    return AnalysisRow(
        id=uuid4(),
        document_id=document_id,
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        statement_days=31,
        beginning_balance=Decimal("0.00"),
        ending_balance=Decimal("0.00"),
        avg_daily_balance=Decimal("0.00"),
        true_revenue=Decimal("0.00"),
        monthly_revenue=Decimal("0.00"),
        lowest_balance=Decimal("0.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0"),
        monthly_breakdown=months,
    )


def _month_row(
    month: str,
    deposits: str,
    *,
    avg_balance: str = "10000.00",
    nsf_count: str = "0",
    withdrawals: str = "0.00",
) -> dict[str, str]:
    return {
        "month": month,
        "deposits": deposits,
        "withdrawals": withdrawals,
        "avg_balance": avg_balance,
        "nsf_count": nsf_count,
    }


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


def test_helper_returns_six_cells_in_calendar_order_with_correct_arrows() -> None:
    """One proceed doc carrying 6 months of breakdown surfaces 6 cells.

    Calendar ASC order, first cell carries no arrow (no comparator),
    remaining cells compare against the prior month with the 2% noise
    floor.
    """
    doc = _make_doc(uploaded_at=datetime(2026, 6, 1, tzinfo=UTC))
    analysis = _make_analysis(
        document_id=doc.id,
        months=[
            _month_row("2026-01", "50000.00", avg_balance="10000.00", nsf_count="0"),
            _month_row("2026-02", "51000.00", avg_balance="11000.00", nsf_count="1"),
            _month_row("2026-03", "60000.00", avg_balance="12000.00", nsf_count="0"),
            _month_row("2026-04", "40000.00", avg_balance="8000.00", nsf_count="3"),
            _month_row("2026-05", "55000.00", avg_balance="9500.00", nsf_count="0"),
            _month_row("2026-06", "70000.00", avg_balance="13000.00", nsf_count="0"),
        ],
    )
    out = _build_merchant_monthly_trend(
        all_docs=[doc],
        analyses_by_doc={doc.id: analysis},
    )

    assert len(out) == 6
    assert [row["month"] for row in out] == [
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
    ]
    assert [row["trend"] for row in out] == ["", "flat", "up", "down", "up", "up"]
    assert out[0]["label"] == "Jan 2026"
    assert out[-1]["label"] == "Jun 2026"
    assert out[3]["nsf_count"] == 3
    assert out[5]["deposits"] == Decimal("70000.00")
    assert out[5]["adb"] == Decimal("13000.00")
    assert out[0]["deposits_source_ids"] == [str(doc.id)]


def test_helper_keeps_only_most_recent_six_months_when_more_exist() -> None:
    """Eight months of breakdown collapse to the most-recent six,
    rendered calendar ASC."""
    doc = _make_doc(uploaded_at=datetime(2026, 8, 1, tzinfo=UTC))
    analysis = _make_analysis(
        document_id=doc.id,
        months=[_month_row(f"2026-{m:02d}", f"{50000 + m * 1000}.00") for m in range(1, 9)],
    )
    out = _build_merchant_monthly_trend(
        all_docs=[doc],
        analyses_by_doc={doc.id: analysis},
    )

    assert len(out) == 6
    assert [row["month"] for row in out] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
    ]


def test_helper_returns_empty_when_no_proceed_docs() -> None:
    """Non-proceed parse status → strip is hidden (helper returns [])."""
    doc = _make_doc(
        parse_status="manual_review",
        uploaded_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    analysis = _make_analysis(
        document_id=doc.id,
        months=[_month_row("2026-01", "50000.00")],
    )
    out = _build_merchant_monthly_trend(
        all_docs=[doc],
        analyses_by_doc={doc.id: analysis},
    )
    assert out == []


def test_helper_returns_empty_when_no_breakdown_rows() -> None:
    """Proceed doc with an empty ``monthly_breakdown`` → strip hidden."""
    doc = _make_doc(uploaded_at=datetime(2026, 6, 1, tzinfo=UTC))
    analysis = _make_analysis(document_id=doc.id, months=[])
    out = _build_merchant_monthly_trend(
        all_docs=[doc],
        analyses_by_doc={doc.id: analysis},
    )
    assert out == []


def test_helper_dedupes_overlapping_months_keeping_freshest_doc() -> None:
    """Two proceed docs cover March (renewal-merchant overlap).

    ``all_docs`` is newest-first upstream. The fresh doc reports
    ``60000`` for March; the older doc reports ``42000``. Dedup keeps
    the fresh value AND tags only the fresh doc as the source.
    """
    fresh_doc = _make_doc(uploaded_at=datetime(2026, 6, 1, tzinfo=UTC))
    older_doc = _make_doc(uploaded_at=datetime(2026, 4, 1, tzinfo=UTC))

    fresh_analysis = _make_analysis(
        document_id=fresh_doc.id,
        months=[
            _month_row("2026-03", "60000.00", avg_balance="12000.00", nsf_count="0"),
            _month_row("2026-04", "65000.00"),
        ],
    )
    older_analysis = _make_analysis(
        document_id=older_doc.id,
        months=[
            _month_row("2026-02", "48000.00"),
            _month_row("2026-03", "42000.00", avg_balance="9000.00", nsf_count="2"),
        ],
    )

    out = _build_merchant_monthly_trend(
        all_docs=[fresh_doc, older_doc],
        analyses_by_doc={
            fresh_doc.id: fresh_analysis,
            older_doc.id: older_analysis,
        },
    )

    by_month = {row["month"]: row for row in out}
    assert by_month["2026-03"]["deposits"] == Decimal("60000.00")
    assert by_month["2026-03"]["adb"] == Decimal("12000.00")
    assert by_month["2026-03"]["nsf_count"] == 0
    assert by_month["2026-03"]["deposits_source_ids"] == [str(fresh_doc.id)]
    assert by_month["2026-02"]["deposits"] == Decimal("48000.00")
    assert by_month["2026-02"]["deposits_source_ids"] == [str(older_doc.id)]


def test_helper_skips_malformed_breakdown_rows() -> None:
    """Rows missing ``month`` or carrying non-numeric values are
    skipped silently rather than crashing the dossier render."""
    doc = _make_doc(uploaded_at=datetime(2026, 6, 1, tzinfo=UTC))
    analysis = _make_analysis(
        document_id=doc.id,
        months=[
            {"deposits": "50000.00", "avg_balance": "1000", "nsf_count": "0"},
            _month_row("2026-02", "not-a-number"),
            _month_row("2026-03", "60000.00"),
        ],
    )
    out = _build_merchant_monthly_trend(
        all_docs=[doc],
        analyses_by_doc={doc.id: analysis},
    )
    assert [row["month"] for row in out] == ["2026-03"]


# ---------------------------------------------------------------------------
# Template-level tests
# ---------------------------------------------------------------------------


def _make_merchant() -> MerchantRow:
    return MerchantRow(
        business_name="Acme Painting LLC",
        owner_name="Jane Owner",
        state="CA",
        industry_naics="722511",
        time_in_business_months=24,
        credit_score=720,
    )


def _render_dossier(*, merchant_monthly_trend: list[dict[str, object]]) -> str:
    """Render the dossier template with the bare minimum context.

    Unused sections collapse to their empty-state branches; we assert
    only against the strip rendered by the new template block.
    """
    template = templates.get_template("merchant_detail_dossier.html.j2")
    return template.render(
        request=None,
        merchant=_make_merchant(),
        documents=[],
        document=None,
        analysis=None,
        aggregate_labels={},
        aggregate_unit_kind={},
        pattern_cards=[],
        latest_transactions=[],
        soft_signals=None,
        has_concentration_pattern=False,
        from_intake=False,
        intake_docs_uploaded=0,
        intake_docs_failed=0,
        score_result=None,
        score_window=None,
        statement_coverage=None,
        stacking=None,
        mca_stack=None,
        balance_health=None,
        offer=None,
        state_tier=None,
        ofac_status="pending",
        ofac_match=None,
        trend=None,
        history=[],
        close_last_orchestration_capped=False,
        unified_tracks=None,
        shadow_signals=[],
        merchant_shadow_signals=[],
        revenue_trends=None,
        merchant_monthly_trend=merchant_monthly_trend,
        funder_note_submissions=[],
        operator_notes=[],
        operator_note_max_chars=2000,
        deal_summary=None,
        funder_narrative="",
        doc_checklist={
            "voided_check_on_file": False,
            "drivers_license_on_file": False,
            "bank_statements_months": 0,
        },
        stips_result=None,
        top_matched_funder_name=None,
        matched_funders=[],
        matched_funder_responses={},
        submitted_funder_ids=set(),
    )


def test_template_renders_strip_when_trend_present() -> None:
    trend: list[dict[str, object]] = [
        {
            "month": "2026-01",
            "label": "Jan 2026",
            "deposits": Decimal("50000.00"),
            "deposits_source_ids": ["doc-1"],
            "adb": Decimal("10000.00"),
            "nsf_count": 0,
            "trend": "",
        },
        {
            "month": "2026-02",
            "label": "Feb 2026",
            "deposits": Decimal("60000.00"),
            "deposits_source_ids": ["doc-1"],
            "adb": Decimal("11000.00"),
            "nsf_count": 2,
            "trend": "up",
        },
    ]
    html = _render_dossier(merchant_monthly_trend=trend)

    assert 'data-test-id="dossier-monthly-strip"' in html
    assert 'data-test-id="dossier-monthly-cell-1"' in html
    assert 'data-test-id="dossier-monthly-cell-2"' in html
    assert 'data-test-id="dossier-monthly-trend-up"' in html
    assert html.count('data-test-id="dossier-monthly-trend-up"') == 1
    assert html.find("Jan 2026") < html.find("Feb 2026")
    assert ">2 NSF<" in html
    assert "Gross deposits" in html
    assert "$50,000" in html
    assert "$60,000" in html


def test_template_hides_strip_when_trend_empty() -> None:
    """Empty list (no proceed doc, no breakdown) → strip is hidden."""
    html = _render_dossier(merchant_monthly_trend=[])

    assert 'data-test-id="dossier-monthly-strip"' not in html
    assert 'data-test-id="dossier-monthly-cell-1"' not in html


def test_template_renders_flat_and_down_arrows() -> None:
    trend: list[dict[str, object]] = [
        {
            "month": "2026-04",
            "label": "Apr 2026",
            "deposits": Decimal("50000.00"),
            "deposits_source_ids": ["d1"],
            "adb": Decimal("10000.00"),
            "nsf_count": 0,
            "trend": "",
        },
        {
            "month": "2026-05",
            "label": "May 2026",
            "deposits": Decimal("50500.00"),
            "deposits_source_ids": ["d1"],
            "adb": Decimal("10100.00"),
            "nsf_count": 0,
            "trend": "flat",
        },
        {
            "month": "2026-06",
            "label": "Jun 2026",
            "deposits": Decimal("30000.00"),
            "deposits_source_ids": ["d1"],
            "adb": Decimal("8000.00"),
            "nsf_count": 0,
            "trend": "down",
        },
    ]
    html = _render_dossier(merchant_monthly_trend=trend)

    assert 'data-test-id="dossier-monthly-trend-flat"' in html
    assert 'data-test-id="dossier-monthly-trend-down"' in html
