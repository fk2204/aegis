"""Sprint 4 Feature 2 — historical-approval boost arithmetic.

Targets ``match_funders._apply_historical_boost`` directly so the
band logic is locked independent of every other matcher gate.
Integration ("does the boost actually reach FunderMatch.match_score
through the full match_funder call?") is covered in
``test_match_funders_historical_boost_integration`` below.

Operator spec (verbatim):
    If approval rate > 60% for similar deals (same industry tier,
    same score tier) add +5 to match_score. If approval rate < 20%,
    subtract 10. Cap final score at 100.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import (
    _HISTORICAL_BOOST_AMOUNT,
    _HISTORICAL_PENALTY_AMOUNT,
    _apply_historical_boost,
    match_funder,
)
from aegis.scoring.models import ScoreInput, ScoreResult


@pytest.mark.parametrize(
    ("rate", "expected_delta"),
    [
        # Strictly above 0.60 — boost band.
        (Decimal("0.61"), _HISTORICAL_BOOST_AMOUNT),
        (Decimal("0.75"), _HISTORICAL_BOOST_AMOUNT),
        (Decimal("1.00"), _HISTORICAL_BOOST_AMOUNT),
        # Edge: 0.60 exactly is NOT a boost (band is open per operator).
        (Decimal("0.60"), 0),
        # Middle band — no adjustment.
        (Decimal("0.45"), 0),
        (Decimal("0.20"), 0),
        # Strictly below 0.20 — penalty band.
        (Decimal("0.19"), _HISTORICAL_PENALTY_AMOUNT),
        (Decimal("0.05"), _HISTORICAL_PENALTY_AMOUNT),
        (Decimal("0.00"), _HISTORICAL_PENALTY_AMOUNT),
    ],
)
def test_boost_bands(rate: Decimal, expected_delta: int) -> None:
    base = 50
    assert _apply_historical_boost(base, rate) == base + expected_delta


def test_none_rate_is_no_op() -> None:
    """No history -> no adjustment. Different from 0% (which IS a
    signal: 'we've tried and gotten declined every time')."""
    assert _apply_historical_boost(75, None) == 75


def test_cap_at_100() -> None:
    """A 96 base + 5 boost would land at 101 — must cap at 100 so
    the FunderMatch ``Field(le=100)`` constraint stays satisfied."""
    assert _apply_historical_boost(96, Decimal("0.80")) == 100
    assert _apply_historical_boost(100, Decimal("0.80")) == 100


def test_floor_at_zero() -> None:
    """A 5 base + (-10) penalty would land at -5 — must floor at 0
    so the FunderMatch ``Field(ge=0)`` constraint stays satisfied."""
    assert _apply_historical_boost(5, Decimal("0.10")) == 0
    assert _apply_historical_boost(0, Decimal("0.10")) == 0


# ---------------------------------------------------------------------------
# Integration — boost actually reaches FunderMatch.match_score AND
# FunderMatch.historical_approval_rate via match_funder().
# ---------------------------------------------------------------------------


def _baseline_deal(**overrides: object) -> ScoreInput:
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="811111",
        industry_choice="Auto Repair / Service",
        time_in_business_months=36,
        credit_score=650,
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        fraud_score=10,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )
    return base.model_copy(update=overrides)


def _baseline_funder(**overrides: object) -> FunderRow:
    base = FunderRow(
        id=uuid4(),
        name="Test Funder",
        active=True,
        min_monthly_revenue=Decimal("50000.00"),
        min_credit_score=600,
        min_months_in_business=12,
        min_avg_daily_balance=Decimal("5000.00"),
        max_nsf_tolerance=3,
        max_positions=2,
        min_advance=Decimal("10000.00"),
        max_advance=Decimal("250000.00"),
    )
    return base.model_copy(update=overrides)


def _baseline_score() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def test_match_funder_records_historical_rate_when_provided() -> None:
    """When the route passes a rate, FunderMatch.historical_approval_rate
    must be set so the dossier can render it."""
    funder = _baseline_funder()
    deal = _baseline_deal()
    score = _baseline_score()
    match = match_funder(
        funder,
        deal,
        score,
        historical_approval_rate=Decimal("0.7500"),
    )
    assert match is not None
    assert match.historical_approval_rate == Decimal("0.7500")


def test_match_funder_records_none_when_no_rate_provided() -> None:
    """Default path — caller has no historical data, FunderMatch
    surfaces None so the dossier renders 'no track record yet'."""
    funder = _baseline_funder()
    deal = _baseline_deal()
    score = _baseline_score()
    match = match_funder(funder, deal, score)
    assert match is not None
    assert match.historical_approval_rate is None


def test_match_funder_high_rate_lifts_score_relative_to_baseline() -> None:
    """End-to-end: same deal, same funder, same score — only the
    historical rate changes. The boost must be visible in
    ``match_score``."""
    funder = _baseline_funder()
    deal = _baseline_deal()
    score = _baseline_score()
    baseline = match_funder(funder, deal, score)
    boosted = match_funder(
        funder,
        deal,
        score,
        historical_approval_rate=Decimal("0.80"),
    )
    assert baseline is not None and boosted is not None
    # Boost applied AFTER base, capped at 100. Either +5 or held at cap.
    assert boosted.match_score >= baseline.match_score
    assert boosted.match_score - baseline.match_score in (
        _HISTORICAL_BOOST_AMOUNT,
        max(0, 100 - baseline.match_score),
    )


def test_match_funder_low_rate_drags_score_relative_to_baseline() -> None:
    """End-to-end: low historical rate must pull match_score down."""
    funder = _baseline_funder()
    deal = _baseline_deal()
    score = _baseline_score()
    baseline = match_funder(funder, deal, score)
    penalised = match_funder(
        funder,
        deal,
        score,
        historical_approval_rate=Decimal("0.10"),
    )
    assert baseline is not None and penalised is not None
    assert penalised.match_score <= baseline.match_score
    assert baseline.match_score - penalised.match_score in (
        -_HISTORICAL_PENALTY_AMOUNT,  # 10
        baseline.match_score,  # floor case
    )
