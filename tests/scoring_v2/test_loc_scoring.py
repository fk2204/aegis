"""Tests for aegis.scoring_v2.loc_scoring.score_loc_deal (2026-07-01 A1e)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.loc_scoring import (
    LOC_MAX_LINE_TO_REVENUE_RATIO,
    LOC_MAX_NSF,
    LOC_MIN_FICO,
    LOC_MIN_TIB_MONTHS,
    score_loc_deal,
)


def _make_merchant(
    *,
    credit_score: int = 700,
    time_in_business_months: int = 24,
    stated_mca_positions: int = 0,
    requested_amount: Decimal | None = Decimal("100000"),
) -> MerchantRow:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Distributors",
        owner_name="Jane Doe",
        state="TX",
        industry_naics="424820",
        industry_choice="Wholesale",
        status="finalized",
        credit_score=credit_score,
        time_in_business_months=time_in_business_months,
        stated_mca_positions=stated_mca_positions,
        requested_amount=requested_amount,
        created_at=now,
        updated_at=now,
    )


def test_loc_low_fico_blocks() -> None:
    r = score_loc_deal(
        _make_merchant(credit_score=LOC_MIN_FICO - 1),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is False
    assert any("FICO" in b for b in r.blockers)


def test_loc_short_tib_blocks() -> None:
    r = score_loc_deal(
        _make_merchant(time_in_business_months=LOC_MIN_TIB_MONTHS - 1),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is False
    assert any("TIB" in b for b in r.blockers)


def test_loc_too_many_nsf_blocks() -> None:
    r = score_loc_deal(
        _make_merchant(),
        true_revenue_monthly=Decimal("50000"),
        nsf_count_3mo=LOC_MAX_NSF + 2,
    )
    assert r.eligible is False
    assert any("NSF" in b for b in r.blockers)


def test_loc_too_many_positions_blocks() -> None:
    r = score_loc_deal(
        _make_merchant(stated_mca_positions=4),
        true_revenue_monthly=Decimal("50000"),
    )
    assert r.eligible is False
    assert any("MCA" in b or "position" in b for b in r.blockers)


def test_loc_eligible_line_calculation_bounded_by_revenue_ratio() -> None:
    revenue = Decimal("50000")
    r = score_loc_deal(_make_merchant(), true_revenue_monthly=revenue)
    assert r.eligible is True
    assert r.recommended_line is not None
    assert r.recommended_line <= revenue * LOC_MAX_LINE_TO_REVENUE_RATIO


def test_loc_referral_lenders_and_rate_range_populated() -> None:
    r = score_loc_deal(_make_merchant(), true_revenue_monthly=Decimal("50000"))
    assert r.eligible is True
    assert r.referral_lenders
    assert r.estimated_rate_low is not None
    assert r.estimated_rate_high is not None
    assert r.estimated_rate_low < r.estimated_rate_high


def test_loc_zero_revenue_blocks() -> None:
    r = score_loc_deal(_make_merchant(), true_revenue_monthly=Decimal("0"))
    assert r.eligible is False
    assert any("revenue" in b.lower() for b in r.blockers)
