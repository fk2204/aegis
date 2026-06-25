"""Per-match fintech-bank soft concern wired through ``match_funder``.

Pairs with the parser-side detector tests in
``tests/parser/test_fintech_banks.py``. The matcher contract:

  * ``match_funder(..., bank_warning="text")`` adds ``"text"`` to
    ``FunderMatch.soft_concerns`` on the returned match.
  * ``match_funder(...)`` without the kwarg (or with ``bank_warning=None``)
    returns an identical shape to pre-warning callers — no extra soft
    concern, no change to ``match_score``, no change to ``reasons``.
  * The warning lands on every match the matcher returns, regardless of
    whether the funder otherwise qualifies — operator wants the caveat
    visible on every funder card, not just the green ones.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult

_WARNING_TEXT = (
    "Merchant banks with Mercury. Verify funder accepts fintech bank accounts before submitting."
)


def _baseline_deal() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="811111",
        industry_choice="Auto Repair / Service",
        time_in_business_months=36,
        credit_score=680,
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


def _qualifying_funder() -> FunderRow:
    return FunderRow(
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


def _hard_fail_funder() -> FunderRow:
    """Funder whose revenue floor is above the deal — guaranteed
    hard fail on revenue. Used to prove the warning lands on
    non-qualifying matches as well as qualifying ones."""
    return FunderRow(
        id=uuid4(),
        name="High-Floor Funder",
        active=True,
        min_monthly_revenue=Decimal("500000.00"),
        min_credit_score=600,
        min_months_in_business=12,
        min_avg_daily_balance=Decimal("5000.00"),
        max_nsf_tolerance=3,
        max_positions=2,
        min_advance=Decimal("10000.00"),
        max_advance=Decimal("250000.00"),
    )


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


def test_bank_warning_appears_in_soft_concerns_on_qualifying_match() -> None:
    """A qualifying match with the bank_warning kwarg surfaces the
    warning text in soft_concerns."""
    match = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
        bank_warning=_WARNING_TEXT,
    )

    assert match is not None
    assert _WARNING_TEXT in match.soft_concerns


def test_bank_warning_appears_in_soft_concerns_on_hard_fail_match() -> None:
    """A non-qualifying match (hard fail on revenue) STILL carries the
    bank_warning in soft_concerns. The operator wants the caveat on
    every funder card, even red ones."""
    match = match_funder(
        _hard_fail_funder(),
        _baseline_deal(),
        _baseline_score(),
        bank_warning=_WARNING_TEXT,
    )

    assert match is not None
    # The matcher unions hard fails + soft concerns into the same list
    # for the caller's convenience; the warning lands at the tail.
    assert _WARNING_TEXT in match.soft_concerns


def test_no_bank_warning_kwarg_leaves_soft_concerns_unchanged() -> None:
    """Default kwarg (``None``) means no fintech warning added — the
    soft_concerns list is identical to a legacy caller's."""
    match = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
    )

    assert match is not None
    assert not any(c.startswith("Merchant banks with") for c in match.soft_concerns)


def test_explicit_none_bank_warning_is_same_as_omitted() -> None:
    """Passing ``bank_warning=None`` explicitly matches the omitted-kwarg
    behaviour — no warning appended."""
    match_with = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
        bank_warning=None,
    )
    match_without = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
    )

    assert match_with is not None
    assert match_without is not None
    # Same set of soft concerns (the funder id varies per call so
    # match_score / reasons stay equivalent — compare soft_concerns).
    assert match_with.soft_concerns == match_without.soft_concerns


def test_bank_warning_does_not_change_match_score() -> None:
    """Decline-discipline assertion. The warning is surface-only — it
    must NOT influence ``match_score``. Same funder + deal + score with
    and without the warning produce the same ``match_score``."""
    funder = _qualifying_funder()
    deal = _baseline_deal()
    score = _baseline_score()

    match_with = match_funder(funder, deal, score, bank_warning=_WARNING_TEXT)
    match_without = match_funder(funder, deal, score)

    assert match_with is not None
    assert match_without is not None
    assert match_with.match_score == match_without.match_score
