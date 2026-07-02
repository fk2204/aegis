"""Tests for aegis.scoring_v2.sba_scoring.score_sba_deal (2026-07-01 A1a)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.sba_scoring import (
    SBA_7A_MIN_FICO,
    SBA_EXPRESS_MIN_FICO,
    SBA_MIN_TIB_MONTHS,
    score_sba_deal,
)


def _make_merchant(
    *,
    credit_score: int = 720,
    time_in_business_months: int = 60,
    monthly_revenue: Decimal | None = Decimal("100000"),
    stated_mca_positions: int = 0,
    requested_amount: Decimal | None = Decimal("250000"),
) -> MerchantRow:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Manufacturing",
        owner_name="Jane Doe",
        state="TX",
        industry_naics="332710",
        industry_choice="Manufacturing",
        status="finalized",
        credit_score=credit_score,
        time_in_business_months=time_in_business_months,
        monthly_revenue=monthly_revenue,
        stated_mca_positions=stated_mca_positions,
        requested_amount=requested_amount,
        created_at=now,
        updated_at=now,
    )


def test_sba_fico_too_low_blocks() -> None:
    result = score_sba_deal(
        _make_merchant(credit_score=SBA_EXPRESS_MIN_FICO - 1),
        true_revenue_monthly=Decimal("100000"),
    )
    assert result.eligible is False
    assert result.program == "none"
    assert any("FICO" in b for b in result.blockers)


def test_sba_tib_too_low_blocks() -> None:
    result = score_sba_deal(
        _make_merchant(time_in_business_months=SBA_MIN_TIB_MONTHS - 1),
        true_revenue_monthly=Decimal("100000"),
    )
    assert result.eligible is False
    assert any("TIB" in b for b in result.blockers)


def test_sba_active_mca_positions_block() -> None:
    result = score_sba_deal(
        _make_merchant(stated_mca_positions=2),
        true_revenue_monthly=Decimal("100000"),
    )
    assert result.eligible is False
    assert any("MCA" in b for b in result.blockers)


def test_sba_eligible_7a() -> None:
    """FICO ≥ 680, revenue ≥ 50k, clean stack → 7(a) program."""
    result = score_sba_deal(
        _make_merchant(credit_score=SBA_7A_MIN_FICO + 5),
        true_revenue_monthly=Decimal("150000"),
    )
    assert result.eligible is True
    assert result.program == "7a"
    assert result.recommended_amount is not None
    assert result.recommended_amount > Decimal("0")
    assert "Live Oak Bank" in result.referral_lenders
    assert result.estimated_rate_low == Decimal("0.065")


def test_sba_eligible_express_with_concern() -> None:
    """FICO between 650 and 679 → express program + soft concern
    about not qualifying for standard 7(a)."""
    result = score_sba_deal(
        _make_merchant(credit_score=670),
        true_revenue_monthly=Decimal("60000"),
    )
    assert result.eligible is True
    assert result.program == "express"
    assert any("7(a)" in c for c in result.soft_concerns)
    assert "Celtic Bank" in result.referral_lenders


def test_sba_zero_revenue_blocks() -> None:
    result = score_sba_deal(
        _make_merchant(monthly_revenue=Decimal("0")),
        true_revenue_monthly=Decimal("0"),
    )
    assert result.eligible is False
    assert any("Zero measured revenue" in b for b in result.blockers)
