"""Test the four stacking branches in match_funders.

Branches (see scoring/match_funders.py docstring):
  1. accepts_stacking=False + positions ≥ 1     -> SOFT (unconfirmed)
  2. max_positions set + positions > max         -> HARD (exceeds_max_positions)
  3. accepts_stacking=True + max_positions=None  -> SOFT (unspecified)
  4. accepts_stacking=False + positions = 0      -> no concern (clean)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _deal_with_positions(positions: int) -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=positions,
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


def _score_b() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("100000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=180,
    )


# Branch 1 -------------------------------------------------------------------


def test_stacking_unconfirmed_soft_when_funder_has_no_optin_and_deal_has_positions() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="Quiet Fund",
        min_monthly_revenue=Decimal("25000.00"),
        accepts_stacking=False,
        max_positions=None,
    )
    match = match_funder(funder, _deal_with_positions(1), _score_b())
    assert match is not None
    assert any(
        c.startswith("stacking_acceptance_unconfirmed") for c in match.soft_concerns
    ), f"expected unconfirmed soft concern; got {match.soft_concerns}"
    assert match.match_score > 0, "should still qualify (soft only)"


# Branch 2 -------------------------------------------------------------------


def test_exceeds_max_positions_hard_fails() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="Max-2 Fund",
        min_monthly_revenue=Decimal("25000.00"),
        accepts_stacking=True,
        max_positions=2,
    )
    match = match_funder(funder, _deal_with_positions(3), _score_b())
    assert match is not None
    assert match.match_score == 0, "hard fail must drop likelihood to 0"
    assert any(c.startswith("exceeds_max_positions") for c in match.soft_concerns)


def test_within_max_positions_no_concern() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="Max-2 Fund",
        min_monthly_revenue=Decimal("25000.00"),
        accepts_stacking=True,
        max_positions=2,
    )
    match = match_funder(funder, _deal_with_positions(1), _score_b())
    assert match is not None
    assert match.match_score > 0
    assert not any(c.startswith("exceeds_max_positions") for c in match.soft_concerns)


# Branch 3 -------------------------------------------------------------------


def test_stacking_max_unspecified_soft_when_optin_but_no_cap() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="Open-Stacking Fund",
        min_monthly_revenue=Decimal("25000.00"),
        accepts_stacking=True,
        max_positions=None,
    )
    # Even with 0 positions, the soft concern fires because the cap is fuzzy.
    match = match_funder(funder, _deal_with_positions(0), _score_b())
    assert match is not None
    assert any(c.startswith("stacking_max_unspecified") for c in match.soft_concerns)


# Branch 4 -------------------------------------------------------------------


def test_clean_first_position_no_stacking_concern() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="No-Stacking Fund",
        min_monthly_revenue=Decimal("25000.00"),
        accepts_stacking=False,
        max_positions=None,
    )
    match = match_funder(funder, _deal_with_positions(0), _score_b())
    assert match is not None
    assert not any(c.startswith("stacking_acceptance_unconfirmed") for c in match.soft_concerns)
    assert not any(c.startswith("exceeds_max_positions") for c in match.soft_concerns)
    assert not any(c.startswith("stacking_max_unspecified") for c in match.soft_concerns)
    assert match.match_score > 0


# Stacking is no longer a hard fail when the policy is just default-False ----


def test_stacking_was_hard_now_soft() -> None:
    """Regression: the legacy 'funder_does_not_accept_stacking' hard fail is gone."""
    funder = FunderRow(
        id=uuid4(),
        name="Default-Policy Fund",
        min_monthly_revenue=Decimal("25000.00"),
        accepts_stacking=False,
        max_positions=None,
    )
    match = match_funder(funder, _deal_with_positions(2), _score_b())
    assert match is not None
    # Match remains qualified (likelihood > 0) — only soft concern present.
    assert match.match_score > 0
    assert not any("funder_does_not_accept_stacking" in c for c in match.soft_concerns)
