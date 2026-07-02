"""Tests for aegis.scoring_v2.equipment_scoring.score_equipment_deal
(2026-07-01 A1b)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.equipment_scoring import (
    EQUIP_MAX_AGE_YEARS,
    EQUIP_MAX_LTV,
    EQUIP_MIN_FICO,
    EQUIP_MIN_TIB_MONTHS,
    score_equipment_deal,
)


def _make_merchant(
    *,
    credit_score: int = 700,
    time_in_business_months: int = 36,
) -> MerchantRow:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Trucking",
        owner_name="John Smith",
        state="TX",
        industry_naics="484121",
        industry_choice="Trucking",
        status="finalized",
        credit_score=credit_score,
        time_in_business_months=time_in_business_months,
        created_at=now,
        updated_at=now,
    )


def test_equipment_fico_too_low_blocks() -> None:
    result = score_equipment_deal(
        _make_merchant(credit_score=EQUIP_MIN_FICO - 1),
        equipment_cost=Decimal("50000"),
        equipment_type="commercial truck",
    )
    assert result.eligible is False
    assert any("FICO" in b for b in result.blockers)


def test_equipment_ineligible_type_blocks() -> None:
    result = score_equipment_deal(
        _make_merchant(),
        equipment_cost=Decimal("50000"),
        equipment_type="Enterprise software license",
    )
    assert result.eligible is False
    assert any("not eligible collateral" in b for b in result.blockers)


def test_equipment_ltv_calculated_correctly() -> None:
    """Eligible merchant with an invoice → max_advance = cost * 0.90."""
    cost = Decimal("100000")
    result = score_equipment_deal(
        _make_merchant(),
        equipment_cost=cost,
        equipment_type="Kenworth T680 tractor",
    )
    assert result.eligible is True
    assert result.max_advance == cost * EQUIP_MAX_LTV
    assert result.ltv == EQUIP_MAX_LTV
    assert result.required_down_payment == cost * Decimal("0.10")
    assert result.term_months_range == (24, 84)


def test_equipment_old_equipment_soft_warning() -> None:
    result = score_equipment_deal(
        _make_merchant(),
        equipment_cost=Decimal("40000"),
        equipment_type="Peterbilt tractor",
        equipment_age_years=EQUIP_MAX_AGE_YEARS + 3,
    )
    assert result.eligible is True
    assert any("Equipment age" in c for c in result.soft_concerns)


def test_equipment_tib_too_low_blocks() -> None:
    result = score_equipment_deal(
        _make_merchant(time_in_business_months=EQUIP_MIN_TIB_MONTHS - 1),
        equipment_cost=Decimal("40000"),
    )
    assert result.eligible is False
    assert any("TIB" in b for b in result.blockers)
