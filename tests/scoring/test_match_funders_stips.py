"""Sprint 6 Track A — structured stipulations on FunderMatch.

Integration coverage: ``match_funder(..., merchant=...)`` populates
``FunderMatch.missing_stips`` and appends a soft concern per
hard-missing stip; ``match_funder(...)`` without the kwarg behaves
identically to pre-Sprint-6 (no missing_stips, no extra soft concerns).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


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
        conditional_requirements=("Voided check required at funding",),
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


def _merchant(*, voided_check: bool = False) -> MerchantRow:
    return MerchantRow(
        business_name="Acme Co",
        state="CA",
        status="finalized",
        voided_check_on_file=voided_check,
    )


# ---------------------------------------------------------------------------
# With merchant kwarg
# ---------------------------------------------------------------------------


def test_missing_voided_check_surfaces_on_match() -> None:
    """Funder requires voided check, merchant doesn't have it ->
    missing_stips populated AND a soft concern prefixed
    'missing stip:' lands on the match."""
    match = match_funder(
        _baseline_funder(),
        _baseline_deal(),
        _baseline_score(),
        merchant=_merchant(voided_check=False),
    )

    assert match is not None
    assert match.missing_stips == ["Voided check required at funding"]
    stip_concerns = [c for c in match.soft_concerns if c.startswith("missing stip:")]
    assert len(stip_concerns) == 1
    assert "Voided check required at funding" in stip_concerns[0]


def test_voided_check_on_file_no_missing_stips() -> None:
    """Merchant has the voided check on file -> no missing_stips, no
    stip-related soft concern."""
    match = match_funder(
        _baseline_funder(),
        _baseline_deal(),
        _baseline_score(),
        merchant=_merchant(voided_check=True),
    )

    assert match is not None
    assert match.missing_stips == []
    stip_concerns = [c for c in match.soft_concerns if c.startswith("missing stip:")]
    assert stip_concerns == []


# ---------------------------------------------------------------------------
# Legacy callers (no merchant kwarg) — identical pre-Sprint-6 behaviour
# ---------------------------------------------------------------------------


def test_legacy_caller_without_merchant_kwarg_has_empty_missing_stips() -> None:
    """Existing callers (deals API, tests that don't yet route the
    merchant) pass no merchant -> identical match shape to before."""
    match = match_funder(
        _baseline_funder(),
        _baseline_deal(),
        _baseline_score(),
    )

    assert match is not None
    assert match.missing_stips == []
    stip_concerns = [c for c in match.soft_concerns if c.startswith("missing stip:")]
    assert stip_concerns == []
