"""Seasonal-trough suppression of ``revenue_declining_15pct+``.

Sprint 5 Track B — tests for :mod:`aegis.scoring_v2.seasonal` plus the
end-to-end wiring through :func:`aegis.scoring.score.score_deal`.

The detector inputs:
    industry_naics (or industry_choice), monthly_breakdown, now.

The contract:
    * Seasonal industry + 3-mo dip > 25% below 12-mo avg + current
      month in trough window → True (suppress -15 penalty).
    * Non-seasonal industry → False.
    * Wrong month → False.
    * Dip too shallow → False.

Fixture style mirrors ``tests/scoring/conftest.py::clean_deal`` — one
canonical ScoreInput, mutate one field per test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.scoring.models import MonthBreakdown, ScoreInput
from aegis.scoring.score import score_deal
from aegis.scoring_v2.seasonal import is_seasonal_trough

# ---------------------------------------------------------------------------
# Helpers — build month_buckets with a controlled Q4 peak / Q1 trough shape.
# ---------------------------------------------------------------------------


def _seasonal_dip_breakdown(
    *,
    months: list[str],
    baseline_deposits: Decimal,
    last3_deposits: Decimal,
) -> list[MonthBreakdown]:
    """Build a monthly_breakdown where the trailing 3 months sit at
    ``last3_deposits`` and everything before sits at ``baseline_deposits``.

    All months use the same constant ``avg_balance`` / ``withdrawals``
    placeholders — the scorer only reads deposits + nsf_count for the
    paths exercised here.
    """
    breakdown: list[MonthBreakdown] = []
    for idx, month in enumerate(months):
        is_last3 = idx >= len(months) - 3
        breakdown.append(
            MonthBreakdown(
                month=month,
                deposits=last3_deposits if is_last3 else baseline_deposits,
                withdrawals=Decimal("50000.00"),
                avg_balance=Decimal("10000.00"),
                nsf_count=0,
            )
        )
    return breakdown


# 15 calendar months ending with Mar-Apr-May (when ``now`` is May) or
# Nov-Dec-Jan (when ``now`` is January). Each test passes ``now``
# explicitly so the trailing-12-mo baseline + last-3 alignment is
# deterministic regardless of when the suite runs.
_FIFTEEN_MONTHS_ENDING_JAN = [
    "2024-11",
    "2024-12",
    "2025-01",
    "2025-02",
    "2025-03",
    "2025-04",
    "2025-05",
    "2025-06",
    "2025-07",
    "2025-08",
    "2025-09",
    "2025-10",
    "2025-11",
    "2025-12",
    "2026-01",
]
_FIFTEEN_MONTHS_ENDING_JUL = [
    "2025-05",
    "2025-06",
    "2025-07",
    "2025-08",
    "2025-09",
    "2025-10",
    "2025-11",
    "2025-12",
    "2026-01",
    "2026-02",
    "2026-03",
    "2026-04",
    "2026-05",
    "2026-06",
    "2026-07",
]
_FIFTEEN_MONTHS_ENDING_FEB = [
    "2024-12",
    "2025-01",
    "2025-02",
    "2025-03",
    "2025-04",
    "2025-05",
    "2025-06",
    "2025-07",
    "2025-08",
    "2025-09",
    "2025-10",
    "2025-11",
    "2025-12",
    "2026-01",
    "2026-02",
]


# ---------------------------------------------------------------------------
# 1. Restaurant + January + deep dip → True
# ---------------------------------------------------------------------------


def test_restaurant_january_deep_dip_is_seasonal_trough() -> None:
    """NAICS 722511 (restaurants) in January with last3 ~60% of 12-mo
    average → is_seasonal_trough is True."""
    breakdown = _seasonal_dip_breakdown(
        months=_FIFTEEN_MONTHS_ENDING_JAN,
        baseline_deposits=Decimal("120000.00"),
        last3_deposits=Decimal("60000.00"),  # 50% below baseline
    )
    result = is_seasonal_trough(
        industry_naics="722511",
        industry_choice=None,
        month_buckets=breakdown,
        now=datetime(2026, 1, 15, tzinfo=UTC),
    )
    assert result is True


# ---------------------------------------------------------------------------
# 2. Restaurant + July + same-shape dip → False (off-season)
# ---------------------------------------------------------------------------


def test_restaurant_july_same_shape_dip_is_not_seasonal_trough() -> None:
    """Same shape, wrong month: July is not in the restaurant trough
    window → returns False, the existing -15 penalty would still fire."""
    breakdown = _seasonal_dip_breakdown(
        months=_FIFTEEN_MONTHS_ENDING_JUL,
        baseline_deposits=Decimal("120000.00"),
        last3_deposits=Decimal("60000.00"),
    )
    result = is_seasonal_trough(
        industry_naics="722511",
        industry_choice=None,
        month_buckets=breakdown,
        now=datetime(2026, 7, 15, tzinfo=UTC),
    )
    assert result is False


# ---------------------------------------------------------------------------
# 3. Software consulting in January + dip → False (non-seasonal industry)
# ---------------------------------------------------------------------------


def test_software_consulting_january_dip_is_not_seasonal_trough() -> None:
    """NAICS 541512 (computer systems design) is not in the seasonal
    map. Even with the right month + dip shape, returns False so the
    -15 penalty fires unchanged."""
    breakdown = _seasonal_dip_breakdown(
        months=_FIFTEEN_MONTHS_ENDING_JAN,
        baseline_deposits=Decimal("120000.00"),
        last3_deposits=Decimal("60000.00"),
    )
    result = is_seasonal_trough(
        industry_naics="541512",
        industry_choice=None,
        month_buckets=breakdown,
        now=datetime(2026, 1, 15, tzinfo=UTC),
    )
    assert result is False


# ---------------------------------------------------------------------------
# 4. Construction in February + deep dip → True
# ---------------------------------------------------------------------------


def test_construction_february_deep_dip_is_seasonal_trough() -> None:
    """NAICS 238210 (specialty trades contractors) in February with last3
    ~50% below baseline → True. Winter freeze hits northern markets."""
    breakdown = _seasonal_dip_breakdown(
        months=_FIFTEEN_MONTHS_ENDING_FEB,
        baseline_deposits=Decimal("180000.00"),
        last3_deposits=Decimal("90000.00"),
    )
    result = is_seasonal_trough(
        industry_naics="238210",
        industry_choice=None,
        month_buckets=breakdown,
        now=datetime(2026, 2, 10, tzinfo=UTC),
    )
    assert result is True


# ---------------------------------------------------------------------------
# 5. Restaurant + January but dip only 10% → False (too shallow)
# ---------------------------------------------------------------------------


def test_restaurant_january_shallow_dip_is_not_seasonal_trough() -> None:
    """Restaurant in January, but last3 sits at 90% of baseline (10%
    dip). Below the 25% threshold → returns False; the trough
    explanation does not fit, so the existing -15 trend penalty path
    runs untouched."""
    breakdown = _seasonal_dip_breakdown(
        months=_FIFTEEN_MONTHS_ENDING_JAN,
        baseline_deposits=Decimal("100000.00"),
        last3_deposits=Decimal("90000.00"),  # 10% below baseline
    )
    result = is_seasonal_trough(
        industry_naics="722511",
        industry_choice=None,
        month_buckets=breakdown,
        now=datetime(2026, 1, 15, tzinfo=UTC),
    )
    assert result is False


# ---------------------------------------------------------------------------
# End-to-end integration — score_deal must suppress the penalty.
# ---------------------------------------------------------------------------


def _restaurant_january_deal(
    *,
    industry_naics: str = "722511",
    industry_choice: str | None = None,
) -> ScoreInput:
    """Build a ScoreInput shaped like a restaurant deal scored in
    January with a seasonal Q4-peak/Q1-trough monthly_breakdown.

    The trend rule sees last3=[Nov, Dec, Jan] where Nov is the peak
    and Jan is the trough — last vs first deposits drop > 15%, so
    without the seasonal suppressor it would add -15
    ``revenue_declining_15pct+`` to the breakdown.
    """
    months_15 = [
        "2024-11",
        "2024-12",
        "2025-01",
        "2025-02",
        "2025-03",
        "2025-04",
        "2025-05",
        "2025-06",
        "2025-07",
        "2025-08",
        "2025-09",
        "2025-10",
        "2025-11",
        "2025-12",
        "2026-01",
    ]
    # Build the shape so:
    #   * The within-last-3-window slope is steep enough to trip the
    #     legacy 15% decline rule (last vs first):
    #       Nov 2025 → $150k, Dec 2025 → $150k, Jan 2026 → $20k.
    #       last/first = 20/150 ≈ -0.87, well below the -0.15 cutoff.
    #   * The trailing-12-month baseline (the 12 months PRECEDING last3,
    #     i.e. Nov 2024 through Oct 2025) sits at $150k each. last-3
    #     average = (150+150+20)/3 = 106.67; baseline_avg = 150:
    #       dip = (150 - 106.67) / 150 ≈ 0.289 → ABOVE the 25% threshold.
    custom_breakdown: list[MonthBreakdown] = []
    for month in months_15:
        if month == "2026-01":
            deposits = Decimal("20000.00")
        else:
            deposits = Decimal("150000.00")
        custom_breakdown.append(
            MonthBreakdown(
                month=month,
                deposits=deposits,
                withdrawals=Decimal("50000.00"),
                avg_balance=Decimal("10000.00"),
                nsf_count=0,
            )
        )

    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Joe's Diner LLC",
        owner_name="Joe Owner",
        state="CA",
        industry_naics=industry_naics,
        industry_choice=industry_choice,
        industry_risk_tier="moderate",
        time_in_business_months=48,
        credit_score=720,
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=True,
        returned_ach_count=0,
        customer_concentration_pct=25,
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        statement_days=31,
        fraud_score=10,
        eof_markers=1,
        validation_passed=True,
        extraction_confidence=95,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        monthly_breakdown=custom_breakdown,
    )


def _breakdown_factors(result: object) -> list[str]:
    """Pull the ``factor`` strings off a ScoreResult.breakdown list."""
    factors: list[str] = []
    for entry in result.breakdown:  # type: ignore[attr-defined]
        factor = entry.get("factor")
        if isinstance(factor, str):
            factors.append(factor)
    return factors


def test_e2e_restaurant_january_suppresses_revenue_declining_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: a restaurant deal scored in January with a seasonal
    Q4→Q1 decline pattern must NOT carry ``revenue_declining_15pct+``
    in its breakdown, and must carry the ``seasonal_trough_expected``
    zero-weight annotation instead."""

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return datetime(2026, 1, 15, tzinfo=UTC)

    monkeypatch.setattr("aegis.scoring_v2.seasonal.datetime", _FixedDatetime)

    deal = _restaurant_january_deal()
    result = score_deal(deal)
    factors = _breakdown_factors(result)

    assert "revenue_declining_15pct+" not in factors, (
        f"seasonal trough should suppress the declining-revenue penalty; "
        f"got breakdown factors: {factors}"
    )
    assert "seasonal_trough_expected" in factors, (
        f"expected seasonal_trough_expected annotation in breakdown; got factors: {factors}"
    )


def test_e2e_software_consulting_january_keeps_revenue_declining_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration mirror: same shape, but the industry is software
    consulting (541512). The seasonal suppressor must NOT fire — the
    legacy ``revenue_declining_15pct+`` penalty applies and the
    ``seasonal_trough_expected`` annotation must NOT appear."""

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return datetime(2026, 1, 15, tzinfo=UTC)

    monkeypatch.setattr("aegis.scoring_v2.seasonal.datetime", _FixedDatetime)

    deal = _restaurant_january_deal(industry_naics="541512")
    result = score_deal(deal)
    factors = _breakdown_factors(result)

    assert "revenue_declining_15pct+" in factors, (
        f"non-seasonal industry should still trigger the declining-revenue "
        f"penalty; got breakdown factors: {factors}"
    )
    assert "seasonal_trough_expected" not in factors, (
        f"non-seasonal industry must not carry the seasonal annotation; got factors: {factors}"
    )
