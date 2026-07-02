"""Tests for aegis.scoring_v2.term_loan_scoring (2026-07-01 A1f)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.term_loan_scoring import (
    TERM_MAX_NSF,
    TERM_MIN_FICO,
    TERM_MIN_TIB_MONTHS,
    TERM_PREFERRED_FICO,
    _estimate_dscr,
    score_term_loan_deal,
)


def _make_merchant(
    *,
    credit_score: int = 720,
    time_in_business_months: int = 36,
    stated_mca_positions: int = 0,
    requested_amount: Decimal | None = Decimal("100000"),
) -> MerchantRow:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Contractors",
        owner_name="Jane Doe",
        state="TX",
        industry_naics="238220",
        industry_choice="Construction",
        status="finalized",
        credit_score=credit_score,
        time_in_business_months=time_in_business_months,
        stated_mca_positions=stated_mca_positions,
        requested_amount=requested_amount,
        created_at=now,
        updated_at=now,
    )


def test_term_low_fico_blocks() -> None:
    r = score_term_loan_deal(
        _make_merchant(credit_score=TERM_MIN_FICO - 1),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is False
    assert any("FICO" in b for b in r.blockers)


def test_term_short_tib_blocks() -> None:
    r = score_term_loan_deal(
        _make_merchant(time_in_business_months=TERM_MIN_TIB_MONTHS - 1),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is False
    assert any("TIB" in b for b in r.blockers)


def test_term_low_dscr_blocks() -> None:
    # $150k loan on $5k/month revenue — DSCR will be far below 1.25.
    r = score_term_loan_deal(
        _make_merchant(requested_amount=Decimal("150000")),
        true_revenue_monthly=Decimal("5000"),
    )
    assert r.eligible is False
    assert any("DSCR" in b for b in r.blockers)


def test_term_bank_tier_for_strong_deal() -> None:
    r = score_term_loan_deal(
        _make_merchant(credit_score=720, time_in_business_months=48),
        true_revenue_monthly=Decimal("80000"),
    )
    assert r.eligible is True
    assert r.tier == "bank"
    assert r.estimated_rate_low is not None
    assert r.estimated_rate_low < Decimal("0.12")


def test_term_alt_lender_tier_for_weaker_deal() -> None:
    # FICO 670: below preferred 700 → alt lender.
    r = score_term_loan_deal(
        _make_merchant(credit_score=670),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is True
    assert r.tier == "alt_lender"


def test_term_dscr_calculation_positive() -> None:
    dscr = _estimate_dscr(
        monthly_revenue=Decimal("50000"),
        requested_amount=Decimal("100000"),
        term_months=36,
        rate=Decimal("0.10"),
    )
    assert dscr > Decimal("1.25")


def test_term_monthly_payment_populated() -> None:
    r = score_term_loan_deal(
        _make_merchant(),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is True
    assert r.estimated_monthly_payment is not None
    assert r.estimated_monthly_payment > Decimal("0")


def test_term_referral_lenders_populated() -> None:
    r = score_term_loan_deal(
        _make_merchant(),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is True
    assert r.referral_lenders


def test_term_too_many_nsf_blocks() -> None:
    r = score_term_loan_deal(
        _make_merchant(),
        true_revenue_monthly=Decimal("50000"),
        nsf_count_3mo=TERM_MAX_NSF + 2,
    )
    assert r.eligible is False
    assert any("NSF" in b for b in r.blockers)


def test_term_preferred_fico_soft_concern_present_when_below_threshold() -> None:
    # FICO 680: passes minimum (660) but below preferred (700) → soft
    # concern about alt-lender rates.
    r = score_term_loan_deal(
        _make_merchant(credit_score=TERM_PREFERRED_FICO - 20),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is True
    assert any("FICO" in c or "alt-lender" in c for c in r.soft_concerns)
