"""match_funder gates driven by FunderRow.operator_status (migration 063).

Covers the 4 states:
  * active                  — default, no matcher impact.
  * paused                  — hard-fail with funder_paused.
  * first_position_only     — hard-fail with funder_first_position_only
                              when deal.mca_positions >= 1; passes when 0.
  * selective               — soft concern funder_selective_appetite.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _deal(**overrides: object) -> ScoreInput:
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="722511",
        time_in_business_months=36,
        credit_score=700,
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


def _score(tier: Literal["A", "B", "C", "D", "F"] = "C") -> ScoreResult:
    return ScoreResult(
        score=60,
        tier=tier,
        recommendation="approve",
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def _funder(**overrides: object) -> FunderRow:
    base = FunderRow(
        id=uuid4(),
        name="Operator-Status Test Funder",
        min_monthly_revenue=Decimal("25000.00"),
        min_credit_score=600,
    )
    return base.model_copy(update=overrides)


def test_active_status_has_no_matcher_impact() -> None:
    """Default state — match runs as if the field didn't exist."""
    funder = _funder(operator_status="active")
    match = match_funder(funder, _deal(), _score())
    assert match is not None
    # No operator_status soft concern emitted.
    assert not any("funder_paused" in c for c in match.soft_concerns)
    assert not any("funder_first_position_only" in c for c in match.soft_concerns)
    assert not any("funder_selective_appetite" in c for c in match.soft_concerns)


def test_paused_status_hard_fails_with_funder_paused_reason() -> None:
    funder = _funder(operator_status="paused")
    match = match_funder(funder, _deal(), _score())
    assert match is not None
    assert match.match_score == 0, "hard fail must zero match_score"
    # funder_paused appears verbatim in soft_concerns (the union of hard + soft).
    assert any(c.startswith("funder_paused:") for c in match.soft_concerns), match.soft_concerns


def test_first_position_only_hard_fails_when_deal_is_stacked() -> None:
    funder = _funder(operator_status="first_position_only")
    deal = _deal(mca_positions=2, mca_daily_total=Decimal("450.00"))
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 0
    assert any(c.startswith("funder_first_position_only:") for c in match.soft_concerns), (
        match.soft_concerns
    )


def test_first_position_only_passes_when_deal_has_no_positions() -> None:
    funder = _funder(operator_status="first_position_only")
    deal = _deal(mca_positions=0, mca_daily_total=Decimal("0.00"))
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any(c.startswith("funder_first_position_only:") for c in match.soft_concerns)


def test_selective_status_adds_soft_concern_without_hard_failing() -> None:
    funder = _funder(operator_status="selective")
    match = match_funder(funder, _deal(), _score())
    assert match is not None
    assert match.match_score > 0, "selective is a soft concern, not a hard fail"
    assert any(c.startswith("funder_selective_appetite:") for c in match.soft_concerns), (
        match.soft_concerns
    )


def test_paused_takes_precedence_over_other_passing_criteria() -> None:
    """A funder that would otherwise be a perfect match still hard-fails on paused.

    Pins the "operator_status fires regardless of underwriting fit" property —
    you don't want the matcher to silently show a paused funder as a
    high-confidence match because the deal happens to clear every other gate.
    """
    funder = _funder(
        operator_status="paused",
        min_monthly_revenue=Decimal("10000.00"),
        min_credit_score=500,
        excluded_states=(),
    )
    match = match_funder(funder, _deal(), _score())
    assert match is not None
    assert match.match_score == 0
    assert any(c.startswith("funder_paused:") for c in match.soft_concerns)
