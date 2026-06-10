"""U37 — per-tier pricing (factor / holdback / payback / daily) tests.

Verifies ``aegis.scoring.pricing.estimate_tier_pricing`` for the four
shapes the matched-funders inline panel renders:

* Tier with both buy_rate bounds + advance → payback / daily populated.
* Tier with no buy_rate (UCS SBA Express, Equipment Financing) → payback
  / daily are ``None`` even when advance is supplied. No fabrication.
* No advance supplied → payback / daily ``None`` regardless of buy_rate.
* Decimal precision sticks to the documented quantizers
  (rates 4dp, money 2dp).

Plus matcher-integration tests:

* Logic Advance Elite (buy_rate 1.18-1.22, max_holdback 0.10,
  max_advance $250k) populates ``estimated_payback_total`` and
  ``estimated_daily_payment`` on ``TierMatch`` when wired through
  ``match_funder``.
* UCS MCA (buy_rate 1.20-1.49) does the same. UCS SBA Express
  (no buy_rate) leaves the new fields ``None``.

And a render-shape "snapshot" via ``model_dump`` so the template's
expected field set stays locked.

All math is Decimal — no float comparisons, no ``pytest.approx``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest

from aegis.funders.models import FunderTier
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult
from aegis.scoring.pricing import (
    DEFAULT_TERM_BUSINESS_DAYS,
    TierPricing,
    estimate_tier_pricing,
)

# ---------------------------------------------------------------------------
# Fixtures (mirror the operator-curated tiers from migration 046)
# ---------------------------------------------------------------------------


def _logic_advance_elite() -> FunderTier:
    """Logic Advance's tightest tier — operator-curated thresholds."""
    return FunderTier(
        name="Elite",
        buy_rate_low=Decimal("1.18"),
        buy_rate_high=Decimal("1.22"),
        min_credit_score=720,
        min_months_in_business=36,
        min_monthly_revenue=Decimal("40000.00"),
        max_positions=0,
        max_advance=Decimal("250000.00"),
        max_holdback=Decimal("0.10"),
    )


def _highland_hill_tier() -> FunderTier:
    """Highland Hill 5-rate ladder representative tier."""
    return FunderTier(
        name="Highland Hill A",
        buy_rate_low=Decimal("1.25"),
        buy_rate_high=Decimal("1.25"),
        min_credit_score=680,
        min_months_in_business=24,
        min_monthly_revenue=Decimal("30000.00"),
        max_positions=1,
        max_advance=Decimal("200000.00"),
        max_holdback=Decimal("0.15"),
    )


def _ucs_sba_express() -> FunderTier:
    """UCS SBA Express tier — has eligibility floors but no buy_rate."""
    return FunderTier(
        name="SBA Express",
        min_credit_score=680,
        min_months_in_business=24,
        min_monthly_revenue=Decimal("40000.00"),
        max_positions=0,
        max_advance=Decimal("500000.00"),
    )


def _ucs_mca() -> FunderTier:
    """UCS MCA product line — wide buy_rate envelope."""
    return FunderTier(
        name="MCA",
        buy_rate_low=Decimal("1.20"),
        buy_rate_high=Decimal("1.49"),
        min_credit_score=500,
        min_months_in_business=4,
        min_monthly_revenue=Decimal("15000.00"),
        max_positions=4,
        max_advance=Decimal("500000.00"),
        max_holdback=Decimal("0.25"),
    )


def _deal(**overrides: object) -> ScoreInput:
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="238320",
        time_in_business_months=48,
        credit_score=720,
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3500.00"),
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


def _score(
    *,
    tier: Literal["A", "B", "C", "D", "F"] = "B",
    suggested_max_advance: Decimal = Decimal("100000.00"),
) -> ScoreResult:
    return ScoreResult(
        score=70,
        tier=tier,
        recommendation="approve",
        suggested_max_advance=suggested_max_advance,
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


# ---------------------------------------------------------------------------
# Unit — estimate_tier_pricing
# ---------------------------------------------------------------------------


def test_logic_advance_elite_with_advance_populates_payback_and_daily() -> None:
    """Buy_rate range + advance → full pricing block.

    Elite: 1.18-1.22 → midpoint 1.20. $100k * 1.20 = $120k payback.
    $120k / 252 business days = $476.19/day (rounded HALF_UP).
    """
    tier = _logic_advance_elite()
    pricing = estimate_tier_pricing(tier, advance=Decimal("100000.00"))

    assert pricing.factor_low == Decimal("1.1800")
    assert pricing.factor_high == Decimal("1.2200")
    assert pricing.holdback_cap == Decimal("0.1000")
    assert pricing.payback_total == Decimal("120000.00")
    # 120000 / 252 = 476.190476... → 476.19 at 2dp HALF_UP.
    assert pricing.daily_payment_estimate == Decimal("476.19")


def test_highland_hill_flat_rate_treats_low_eq_high_as_midpoint() -> None:
    """Highland Hill publishes ``buy_rate_low == buy_rate_high == 1.25``.

    Midpoint is just 1.25; $100k * 1.25 = $125k payback;
    $125k / 252 = $496.03/day.
    """
    tier = _highland_hill_tier()
    pricing = estimate_tier_pricing(tier, advance=Decimal("100000.00"))

    assert pricing.factor_low == Decimal("1.2500")
    assert pricing.factor_high == Decimal("1.2500")
    assert pricing.holdback_cap == Decimal("0.1500")
    assert pricing.payback_total == Decimal("125000.00")
    assert pricing.daily_payment_estimate == Decimal("496.03")


def test_tier_without_buy_rate_returns_none_for_payback_and_daily() -> None:
    """UCS SBA Express omits buy_rate — payback / daily must be None even
    with an advance. Holdback also None because the tier omits it."""
    tier = _ucs_sba_express()
    pricing = estimate_tier_pricing(tier, advance=Decimal("100000.00"))

    assert pricing.factor_low is None
    assert pricing.factor_high is None
    assert pricing.holdback_cap is None
    assert pricing.payback_total is None
    assert pricing.daily_payment_estimate is None


def test_pricing_without_advance_omits_dependent_fields() -> None:
    """No advance → no payback / daily, even when buy_rate exists. The
    static fields (factor_low/high, holdback_cap) still surface."""
    tier = _logic_advance_elite()
    pricing = estimate_tier_pricing(tier, advance=None)

    assert pricing.factor_low == Decimal("1.1800")
    assert pricing.factor_high == Decimal("1.2200")
    assert pricing.holdback_cap == Decimal("0.1000")
    assert pricing.payback_total is None
    assert pricing.daily_payment_estimate is None


def test_zero_advance_treated_as_missing() -> None:
    """Decimal('0') advance has no economic meaning — guard against the
    operator passing ``score.suggested_max_advance`` when scoring
    hard-declined and emitted 0.00."""
    tier = _logic_advance_elite()
    pricing = estimate_tier_pricing(tier, advance=Decimal("0.00"))
    assert pricing.payback_total is None
    assert pricing.daily_payment_estimate is None


def test_decimal_precision_quantization_matches_documented_quantizers() -> None:
    """Rates 4dp, money 2dp — verify a tier with longer raw precision is
    rounded HALF_UP to the documented places."""
    tier = FunderTier(
        name="Precision Test",
        buy_rate_low=Decimal("1.23456"),
        buy_rate_high=Decimal("1.27654"),
        max_holdback=Decimal("0.16667"),
        max_advance=Decimal("100000.00"),
    )
    pricing = estimate_tier_pricing(tier, advance=Decimal("10000.00"))

    assert pricing.factor_low == Decimal("1.2346")
    assert pricing.factor_high == Decimal("1.2765")
    assert pricing.holdback_cap == Decimal("0.1667")
    # midpoint(1.23456, 1.27654) = 1.25555; * 10000 = 12555.50.
    assert pricing.payback_total == Decimal("12555.50")
    # 12555.50 / 252 = 49.8234...; HALF_UP → 49.82.
    assert pricing.daily_payment_estimate == Decimal("49.82")


def test_default_term_business_days_is_252() -> None:
    """MCA convention — pin the constant so a future bump is deliberate."""
    assert DEFAULT_TERM_BUSINESS_DAYS == 252


def test_tier_pricing_is_strict_pydantic_model() -> None:
    """``extra='forbid'`` — unknown fields must raise. Guards the dump
    contract the template depends on."""
    with pytest.raises(ValueError):
        TierPricing(  # type: ignore[call-arg]
            factor_low=Decimal("1.20"),
            unknown_field="boom",
        )


# ---------------------------------------------------------------------------
# Matcher integration — match_funder surfaces pricing on TierMatch
# ---------------------------------------------------------------------------


def _logic_advance_funder_with_tiers() -> object:
    """Subset Logic Advance funder used by the integration tests."""
    from aegis.funders.models import FunderRow

    return FunderRow(
        id=uuid4(),
        name="Logic Advance",
        active=True,
        min_credit_score=550,
        min_months_in_business=6,
        min_monthly_revenue=Decimal("20000.00"),
        max_positions=3,
        typical_factor_low=Decimal("1.18"),
        typical_factor_high=Decimal("1.45"),
        typical_holdback_low=Decimal("0.10"),
        typical_holdback_high=Decimal("0.22"),
        max_advance=Decimal("250000.00"),
        tiers=(_logic_advance_elite(),),
    )


def _ucs_funder_with_tiers() -> object:
    """UCS funder with one priced tier (MCA) and one unpriced (SBA)."""
    from aegis.funders.models import FunderRow

    return FunderRow(
        id=uuid4(),
        name="United Capital Source",
        active=True,
        min_credit_score=500,
        min_months_in_business=4,
        min_monthly_revenue=Decimal("15000.00"),
        max_positions=4,
        tiers=(_ucs_mca(), _ucs_sba_express()),
    )


def test_match_funder_populates_payback_on_qualifying_elite_tier() -> None:
    """End-to-end: Logic Advance Elite + strong merchant → match_funder
    returns a TierMatch with payback + daily populated against the clamped
    advance ($100k from the score, below Elite's $250k ceiling)."""
    from aegis.funders.models import FunderRow

    funder = _logic_advance_funder_with_tiers()
    assert isinstance(funder, FunderRow)  # narrow for mypy
    deal = _deal(
        credit_score=760, time_in_business_months=60,
        monthly_revenue=Decimal("80000.00"), mca_positions=0,
    )
    match = match_funder(funder, deal, _score(tier="A"))
    assert match is not None
    assert len(match.tier_matches) == 1
    elite = match.tier_matches[0]

    assert elite.tier_name == "Elite"
    assert elite.qualifies is True
    # midpoint(1.18, 1.22) = 1.20; * $100k = $120k payback.
    assert elite.estimated_payback_total == Decimal("120000.00")
    assert elite.estimated_daily_payment == Decimal("476.19")


def test_match_funder_ucs_mca_has_pricing_sba_does_not() -> None:
    """UCS: MCA tier has buy_rate → pricing populated. SBA Express has no
    buy_rate → pricing fields are None on the TierMatch."""
    from aegis.funders.models import FunderRow

    funder = _ucs_funder_with_tiers()
    assert isinstance(funder, FunderRow)
    deal = _deal(
        credit_score=620, time_in_business_months=12,
        monthly_revenue=Decimal("30000.00"), mca_positions=2,
    )
    match = match_funder(funder, deal, _score(tier="C"))
    assert match is not None

    by_name = {tm.tier_name: tm for tm in match.tier_matches}
    # MCA tier has buy_rate → payback + daily populated.
    mca = by_name["MCA"]
    # midpoint(1.20, 1.49) = 1.345; * $100k = $134,500.
    assert mca.estimated_payback_total == Decimal("134500.00")
    # 134500 / 252 = 533.7301... → 533.73.
    assert mca.estimated_daily_payment == Decimal("533.73")

    # SBA Express: no buy_rate → payback/daily None even though it has
    # max_advance.
    sba = by_name["SBA Express"]
    assert sba.estimated_payback_total is None
    assert sba.estimated_daily_payment is None


def test_clamped_advance_drives_pricing_when_tier_max_below_score() -> None:
    """When score.suggested_max_advance ($300k) exceeds the tier's
    max_advance ($250k), payback is computed against the clamped value."""
    from aegis.funders.models import FunderRow

    funder = _logic_advance_funder_with_tiers()
    assert isinstance(funder, FunderRow)
    deal = _deal(
        credit_score=760, time_in_business_months=60,
        monthly_revenue=Decimal("80000.00"),
    )
    match = match_funder(
        funder, deal, _score(tier="A", suggested_max_advance=Decimal("300000.00"))
    )
    assert match is not None
    elite = match.tier_matches[0]

    # Clamped to Elite.max_advance = $250k. midpoint(1.18, 1.22) = 1.20;
    # * $250k = $300k payback; / 252 = 1190.476... → $1,190.48/day.
    assert elite.estimated_advance == Decimal("250000.00")
    assert elite.estimated_payback_total == Decimal("300000.00")
    assert elite.estimated_daily_payment == Decimal("1190.48")


# ---------------------------------------------------------------------------
# Render-shape "snapshot" via model_dump — locks the field contract
# the template iterates over.
# ---------------------------------------------------------------------------


def test_tier_pricing_dump_shape_is_stable() -> None:
    """The template renders ``factor_low``, ``factor_high``,
    ``holdback_cap``, ``payback_total``, ``daily_payment_estimate``.
    Lock that contract — adding a field requires updating this test
    (and the template) deliberately."""
    pricing = estimate_tier_pricing(
        _logic_advance_elite(), advance=Decimal("100000.00")
    )
    dump = pricing.model_dump()
    assert set(dump.keys()) == {
        "factor_low",
        "factor_high",
        "holdback_cap",
        "payback_total",
        "daily_payment_estimate",
    }
    # Stringified Decimal shape — keep templates from drifting away from
    # Decimal-comparable values.
    assert dump["factor_low"] == Decimal("1.1800")
    assert dump["payback_total"] == Decimal("120000.00")
    assert dump["daily_payment_estimate"] == Decimal("476.19")
