"""Multi-month scoring aggregation — verify how N analyses fold into one ScoreInput.

The KYC merchant exposed the gap: scoring only the latest statement let a
clean March mask wash-deposit + counterparty concentration from earlier
months. ``_score_input_multi_month`` is what closes that hole. These tests
lock the per-metric reduction rules so a future refactor can't quietly
revert to single-month behaviour.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring.multi_month import (
    detect_missing_months,
)
from aegis.scoring.multi_month import (
    score_input_multi_month as _score_input_multi_month,
)
from aegis.storage import AnalysisRow, DocumentRow


def _doc(*, parse_status: str = "proceed", fraud_score: int = 5) -> DocumentRow:
    return DocumentRow(
        id=uuid4(),
        file_hash="a" * 64,
        byte_size=2048,
        original_filename=f"{parse_status}.pdf",
        parse_status=parse_status,
        fraud_score=fraud_score,
        uploaded_at=datetime.now(UTC),
    )


def _analysis(
    *,
    period_start: date,
    period_end: date,
    true_revenue: Decimal,
    adb: Decimal = Decimal("15000.00"),
    lowest: Decimal = Decimal("1000.00"),
    num_nsf: int = 0,
    days_negative: int = 0,
    mca_positions: int = 0,
    mca_daily_total: Decimal = Decimal("0.00"),
    debt_to_revenue: Decimal = Decimal("0.10"),
    payroll_detected: bool = True,
    returned_ach_count: int = 0,
    statement_days: int = 30,
    monthly_breakdown: list[dict[str, str]] | None = None,
) -> AnalysisRow:
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        statement_period_start=period_start,
        statement_period_end=period_end,
        statement_days=statement_days,
        beginning_balance=Decimal("10000.00"),
        ending_balance=Decimal("11000.00"),
        avg_daily_balance=adb,
        true_revenue=true_revenue,
        monthly_revenue=true_revenue,
        lowest_balance=lowest,
        num_nsf=num_nsf,
        days_negative=days_negative,
        mca_positions=mca_positions,
        mca_daily_total=mca_daily_total,
        debt_to_revenue=debt_to_revenue,
        payroll_detected=payroll_detected,
        returned_ach_count=returned_ach_count,
        monthly_breakdown=monthly_breakdown or [],
    )


def _mb(month: str) -> dict[str, str]:
    """Tiny monthly_breakdown entry — only ``month`` matters for gap detection."""
    return {"month": month, "deposits": "0.00", "withdrawals": "0.00", "avg_balance": "0.00"}


def _merchant() -> MerchantRow:
    return MerchantRow(business_name="Acme Inc", owner_name="Jane Doe", state="CA")


def test_multi_month_sums_revenue_and_window_days() -> None:
    """true_revenue + statement_days are summed across the window so the
    score sees the trailing-3-month total, not a single month's slice."""
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000.00"),
        )),
        (_doc(), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("25000.00"), statement_days=28,
        )),
        (_doc(), _analysis(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            true_revenue=Decimal("20000.00"),
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.true_revenue == Decimal("75000.00")
    assert out.statement_days == 88
    # period spans earliest start → latest end
    assert out.statement_period_start == date(2026, 1, 1)
    assert out.statement_period_end == date(2026, 3, 31)
    # monthly_revenue normalized — ~25.6k/mo from 75k / 88d * 30
    assert out.monthly_revenue == Decimal("25568.18")


def test_multi_month_sums_nsf_and_negative_days() -> None:
    """Stress counters (num_nsf, days_negative, returned_ach_count) sum
    across the window — total stress, not most-recent."""
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
            num_nsf=0, days_negative=0, returned_ach_count=1,
        )),
        (_doc(), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"),
            num_nsf=3, days_negative=4, returned_ach_count=2,
        )),
        (_doc(), _analysis(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            true_revenue=Decimal("30000"),
            num_nsf=5, days_negative=2, returned_ach_count=4,
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.num_nsf == 8
    assert out.days_negative == 6
    assert out.returned_ach_count == 7


def test_multi_month_picks_max_for_mca_positions_and_fraud() -> None:
    """Worst-month wins for mca_positions + fraud_score — underwriting
    risk should never be diluted by a quieter recent month."""
    items = [
        (_doc(fraud_score=12), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"), mca_positions=1,
        )),
        (_doc(fraud_score=27), _analysis(  # worst fraud month
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"), mca_positions=3,  # worst MCA
        )),
        (_doc(fraud_score=4), _analysis(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            true_revenue=Decimal("30000"), mca_positions=0,
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.mca_positions == 3
    assert out.fraud_score == 27


def test_multi_month_uses_latest_for_current_state() -> None:
    """mca_daily_total + payroll_detected reflect *current* obligation /
    employment state, so we take the most-recent month, not aggregate."""
    items = [
        (_doc(), _analysis(  # latest (newest)
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
            mca_daily_total=Decimal("450.00"),
            payroll_detected=False,
        )),
        (_doc(), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"),
            mca_daily_total=Decimal("1200.00"),  # older — should NOT win
            payroll_detected=True,
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.mca_daily_total == Decimal("450.00")
    assert out.payroll_detected is False


def test_multi_month_validation_passed_when_docs_have_analyses() -> None:
    """``parse_status="manual_review"`` does NOT fail validation: it means
    classification wanted operator review, NOT that reconciliation failed.
    Every item in the multi-month builder already has an analysis row
    attached (the collector filters out docs without one), so the math
    cleared the validation gate. Regression: prior code flipped
    validation_passed=False on manual_review, which fired
    ``validation_failed_manual_review_required`` as a hard decline on
    VU Development (2026-06) even though revenue was correctly extracted.
    """
    items = [
        (_doc(parse_status="proceed"), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
        )),
        (_doc(parse_status="manual_review"), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"),
        )),
        (_doc(parse_status="review"), _analysis(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            true_revenue=Decimal("30000"),
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.validation_passed is True


def test_multi_month_validation_passed_false_on_real_failure_status() -> None:
    """Only truly-failed states (``error``, ``pending``) flip validation
    off. Defensive: such docs shouldn't reach the multi-month builder
    in practice (they have no analysis row), but if a test seeds one
    in, surface the failure."""
    items = [
        (_doc(parse_status="proceed"), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
        )),
        (_doc(parse_status="error"), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"),
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.validation_passed is False


def test_multi_month_lowest_balance_picks_worst_month() -> None:
    """``lowest_balance`` reflects the worst observed month across the
    window, not the average. Regression: prior code took the mean which
    masked near-zero liquidity events behind quiet averages (VU
    Development 2026-06: mean=$162K hid min=$3,257)."""
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"), lowest=Decimal("327000.00"),
        )),
        (_doc(), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"), lowest=Decimal("5725.55"),
        )),
        (_doc(), _analysis(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            true_revenue=Decimal("30000"), lowest=Decimal("3256.76"),
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.lowest_balance == Decimal("3256.76")


def test_multi_month_single_item_is_equivalent_to_single_month() -> None:
    """N=1 → totals == single-month — caller can use the multi function
    everywhere without a special case."""
    a = _analysis(
        period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
        true_revenue=Decimal("42000.00"),
        num_nsf=2, days_negative=1, mca_positions=2,
        mca_daily_total=Decimal("250.00"),
    )
    items = [(_doc(fraud_score=18), a)]
    out = _score_input_multi_month(_merchant(), items)
    assert out.true_revenue == Decimal("42000.00")
    assert out.num_nsf == 2
    assert out.days_negative == 1
    assert out.mca_positions == 2
    assert out.fraud_score == 18
    assert out.statement_days == 30


def test_detect_missing_months_returns_empty_for_no_gaps() -> None:
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2026-03")],
        )),
        (_doc(), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2026-02")],
        )),
        (_doc(), _analysis(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2026-01")],
        )),
    ]
    assert detect_missing_months(items) == []


def test_detect_missing_months_flags_single_gap() -> None:
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2026-03")],
        )),
        (_doc(), _analysis(
            period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2026-01")],
        )),
    ]
    assert detect_missing_months(items) == ["2026-02"]


def test_detect_missing_months_flags_multiple_gaps_across_year_boundary() -> None:
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2026-02")],
        )),
        (_doc(), _analysis(
            period_start=date(2025, 11, 1), period_end=date(2025, 11, 30),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2025-11")],
        )),
    ]
    assert detect_missing_months(items) == ["2025-12", "2026-01"]


def test_detect_missing_months_handles_single_statement() -> None:
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
            monthly_breakdown=[_mb("2026-03")],
        )),
    ]
    assert detect_missing_months(items) == []


def test_detect_missing_months_returns_empty_for_empty_input() -> None:
    assert detect_missing_months([]) == []


def test_multi_month_means_adb_and_dtr() -> None:
    """avg_daily_balance + debt_to_revenue average across months — a 3k
    ADB month + a 30k ADB month should land around 16.5k, not max'd to 30k."""
    items = [
        (_doc(), _analysis(
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            true_revenue=Decimal("30000"),
            adb=Decimal("30000.00"), debt_to_revenue=Decimal("0.20"),
        )),
        (_doc(), _analysis(
            period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
            true_revenue=Decimal("30000"),
            adb=Decimal("3000.00"), debt_to_revenue=Decimal("0.40"),
        )),
    ]
    out = _score_input_multi_month(_merchant(), items)
    assert out.avg_daily_balance == Decimal("16500.00")
    assert out.debt_to_revenue == Decimal("0.3000")
