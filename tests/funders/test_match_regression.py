"""Regression: match_funder() works the same after FunderRow moved
from a local dataclass in scoring/match_funders.py to a Pydantic model
in funders/models.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import FunderRow as ReExportedFunderRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def test_funder_row_re_export_is_same_class() -> None:
    """Existing imports `from aegis.scoring.match_funders import FunderRow` keep working."""
    assert ReExportedFunderRow is FunderRow


def _baseline_score_input() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("100000.00"),
        monthly_revenue=Decimal("100000.00"),
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


def _baseline_score_result() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("100000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=180,
    )


def test_match_funder_qualifies_clean_deal() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="Test Funder",
        min_monthly_revenue=Decimal("25000.00"),
        min_avg_daily_balance=Decimal("5000.00"),
        min_credit_score=580,
        max_positions=2,
    )
    deal = _baseline_score_input().model_copy(update={"credit_score": 700})
    score = _baseline_score_result()
    match = match_funder(funder, deal, score)
    assert match is not None
    assert match.match_score > 0
    assert match.soft_concerns == []


def test_match_funder_returns_none_when_no_criteria() -> None:
    funder = FunderRow(id=uuid4(), name="Configless Fund")
    deal = _baseline_score_input()
    score = _baseline_score_result()
    assert match_funder(funder, deal, score) is None


def test_missing_credit_becomes_soft_concern() -> None:
    """Funder requires credit but ScoreInput.credit_score is None → soft concern, not hard fail."""
    funder = FunderRow(id=uuid4(), name="Credit-Required Fund", min_credit_score=600)
    deal = _baseline_score_input().model_copy(update={"credit_score": None})
    score = _baseline_score_result()
    match = match_funder(funder, deal, score)
    assert match is not None
    assert "credit_score_unknown" in match.soft_concerns


def test_excluded_state_hard_fails() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="No-CA Fund",
        min_monthly_revenue=Decimal("25000.00"),
        excluded_states=("CA", "NY"),
    )
    deal = _baseline_score_input()
    score = _baseline_score_result()
    match = match_funder(funder, deal, score)
    assert match is not None
    assert match.match_score == 0
    assert any("state_excluded" in c for c in match.soft_concerns)


def test_ny_merchant_funder_missing_compensation_text_hard_fails() -> None:
    """NY § 600.21(f) guard fires when funder lacks compensation text.

    Plan 2.2 wire-in: NY merchant + funder.aegis_compensation_disclosure_text=""
    surfaces as a hard-fail so the operator updates the funder row via
    /ui/funders before the § 600.21(f) letter pipeline runs.
    """
    funder = FunderRow(
        id=uuid4(),
        name="Empty-Disclosure Fund",
        min_monthly_revenue=Decimal("25000.00"),
        # aegis_compensation_disclosure_text defaults to ""
    )
    deal = _baseline_score_input().model_copy(update={"state": "NY"})
    score = _baseline_score_result()
    match = match_funder(funder, deal, score)
    assert match is not None
    assert match.match_score == 0, (
        f"NY merchant must hard-fail when funder has no comp text; "
        f"got soft_concerns={match.soft_concerns!r}"
    )
    assert any(
        "broker_compensation_text_missing" in c for c in match.soft_concerns
    )


def test_ny_merchant_funder_with_compensation_text_passes() -> None:
    """NY § 600.21(f) guard passes silently when funder has text on file."""
    funder = FunderRow(
        id=uuid4(),
        name="Disclosed Fund",
        min_monthly_revenue=Decimal("25000.00"),
        aegis_compensation_disclosure_text=(
            "Commera Capital receives a 5% commission paid by the funder "
            "at funding. No fees are charged to the recipient."
        ),
    )
    deal = _baseline_score_input().model_copy(update={"state": "NY"})
    score = _baseline_score_result()
    match = match_funder(funder, deal, score)
    assert match is not None
    assert match.match_score > 0, (
        f"NY merchant with disclosed funder must qualify; "
        f"got soft_concerns={match.soft_concerns!r}"
    )
    assert not any(
        "broker_compensation" in c for c in match.soft_concerns
    )


def test_non_ny_merchant_skips_broker_compensation_guard() -> None:
    """Non-NY merchant pairs with empty-disclosure funder without firing.

    The guard is currently NY-only per
    aegis.compliance.broker_compensation._STATE_RULES. CA / TX / FL
    merchants must pass through silently regardless of disclosure text.
    """
    funder = FunderRow(
        id=uuid4(),
        name="Empty-Disclosure Fund",
        min_monthly_revenue=Decimal("25000.00"),
        # aegis_compensation_disclosure_text defaults to ""
    )
    # _baseline_score_input is CA — non-NY.
    deal = _baseline_score_input()
    score = _baseline_score_result()
    match = match_funder(funder, deal, score)
    assert match is not None
    assert not any(
        "broker_compensation" in c for c in match.soft_concerns
    )
