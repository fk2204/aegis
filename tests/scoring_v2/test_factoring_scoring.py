"""Tests for aegis.scoring_v2.factoring_scoring.score_factoring_deal
(2026-07-01 A1c)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.factoring_scoring import (
    FACTORING_ADVANCE_RATE,
    FACTORING_MIN_ELIGIBLE_AR,
    score_factoring_deal,
)


def _make_merchant() -> MerchantRow:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Staffing",
        owner_name="Sarah Lee",
        state="TX",
        industry_naics="561320",
        industry_choice="Staffing",
        status="finalized",
        credit_score=650,
        time_in_business_months=36,
        created_at=now,
        updated_at=now,
    )


def test_factoring_no_ar_blocks() -> None:
    result = score_factoring_deal(_make_merchant())
    assert result.eligible is False
    assert any("No A/R aging report" in b for b in result.blockers)


def test_factoring_eligible_advance_calculated() -> None:
    """current 60k + 30-60 20k → 80k eligible * 0.85 → 68k advance."""
    result = score_factoring_deal(
        _make_merchant(),
        ar_current=Decimal("60000"),
        ar_30_60=Decimal("20000"),
    )
    assert result.eligible is True
    assert result.eligible_receivables == Decimal("80000")
    assert result.advance_rate == FACTORING_ADVANCE_RATE
    assert result.estimated_advance == Decimal("80000") * FACTORING_ADVANCE_RATE


def test_factoring_concentration_soft_warning() -> None:
    result = score_factoring_deal(
        _make_merchant(),
        ar_current=Decimal("100000"),
        largest_customer_pct=Decimal("0.65"),
    )
    assert result.eligible is True
    assert any("concentration" in c.lower() for c in result.soft_concerns)


def test_factoring_stale_ar_soft_warning() -> None:
    """90+ share above 20% of eligible AR → concern."""
    result = score_factoring_deal(
        _make_merchant(),
        ar_current=Decimal("50000"),
        ar_30_60=Decimal("0"),
        ar_90_plus=Decimal("20000"),
    )
    assert result.eligible is True
    assert any("90+ day" in c for c in result.soft_concerns)


def test_factoring_below_minimum_blocks() -> None:
    below = FACTORING_MIN_ELIGIBLE_AR - Decimal("1000")
    result = score_factoring_deal(
        _make_merchant(),
        ar_current=below,
    )
    assert result.eligible is False
    assert any("below minimum" in b for b in result.blockers)
