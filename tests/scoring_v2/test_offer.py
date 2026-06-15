"""Tests for ``aegis.scoring_v2.offer.compute_offer``.

Coverage per the spec the operator handed over:

* clean merchant no stack
* merchant with existing stack that reduces capacity
* combined holdback above 35% (discount fires)
* holdback capacity too tight (offer floors out, returns None)
* result below 5000 (returns None)
* rounding to nearest $500

Plus structural guards: holdback_pct stays at the constant default,
max_amount is never below recommended_amount, the no-revenue and
no-capacity short-circuits both return None.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.scoring_v2.mca_stack import MCAStackAggregation
from aegis.scoring_v2.offer import (
    DEFAULT_HOLDBACK_PCT,
    HIGH_COMBINED_HOLDBACK_THRESHOLD,
    MIN_OFFER_FLOOR,
    OfferRecommendation,
    compute_offer,
)


def _mca_stack(
    *,
    active_mca_count: int = 0,
    mca_monthly_load: Decimal = Decimal("0.00"),
    estimated_combined_holdback_pct: Decimal | None = None,
    largest_single_mca_monthly: Decimal = Decimal("0.00"),
    largest_single_mca_lender: str | None = None,
) -> MCAStackAggregation:
    """Build an ``MCAStackAggregation`` for the offer tests.

    Source-id tuples and shadow triggers default to empty — the offer
    logic only reads ``mca_monthly_load`` and
    ``estimated_combined_holdback_pct``, so the rest of the model is
    structural padding for the strict-extra Pydantic config.
    """
    return MCAStackAggregation(
        active_mca_count=active_mca_count,
        active_mca_source_ids=(),
        mca_monthly_load=mca_monthly_load,
        mca_monthly_load_source_ids=(),
        estimated_combined_holdback_pct=estimated_combined_holdback_pct,
        largest_single_mca_monthly=largest_single_mca_monthly,
        largest_single_mca_lender=largest_single_mca_lender,
        largest_single_mca_source_ids=(),
        shadow_triggers=(),
    )


# ─────────────────────────────────────────────────────────────────────
# Case 1 — clean merchant, no existing stack
# ─────────────────────────────────────────────────────────────────────


def test_clean_merchant_no_stack_sizes_at_revenue_multiple() -> None:
    """No existing stack, ample capacity → straight revenue multiple."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("12500"),
        mca_stack=_mca_stack(),  # active_mca_count=0, mca_monthly_load=0
    )
    assert isinstance(offer, OfferRecommendation)
    # capacity_cap = 12500 / 0.15 = 83333.33; both base + max land below.
    assert offer.recommended_amount == Decimal("50000.00")
    assert offer.max_amount == Decimal("75000.00")
    assert offer.holdback_pct == DEFAULT_HOLDBACK_PCT
    # No capacity cap, no overload discount → plain revenue-multiple rationale.
    assert "1.0x monthly revenue" in offer.rationale
    assert "capped" not in offer.rationale
    assert "discounted" not in offer.rationale


# ─────────────────────────────────────────────────────────────────────
# Case 2 — existing stack reduces capacity
# ─────────────────────────────────────────────────────────────────────


def test_existing_stack_reduces_max_amount_via_capacity_cap() -> None:
    """Existing $5,000/mo load eats half the capacity → max_amount
    drops below 1.5x revenue. Recommended (1.0x) still fits."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("30000"),
        holdback_capacity_monthly=Decimal("10000"),
        mca_stack=_mca_stack(
            active_mca_count=2,
            mca_monthly_load=Decimal("5000.00"),
            estimated_combined_holdback_pct=Decimal("16.67"),  # 5k / 30k
        ),
    )
    assert offer is not None
    # remaining = 10000 - 5000 = 5000; capacity_cap = 5000 / 0.15 = 33333.33
    # Round 33333.33 → 33500 (66.67 → 67 x 500).
    # Base (30000) < cap → 30000. Max (45000) > cap → 33333.33 → rounds to 33500.
    assert offer.recommended_amount == Decimal("30000.00")
    assert offer.max_amount == Decimal("33500.00")
    assert "capped" in offer.rationale
    assert "holdback capacity" in offer.rationale


# ─────────────────────────────────────────────────────────────────────
# Case 3 — combined holdback above 35% triggers discount
# ─────────────────────────────────────────────────────────────────────


def test_combined_holdback_above_35_pct_fires_25_pct_discount() -> None:
    """Existing combined holdback at 40% → recommended drops 25%.
    Max stays at the capacity cap (overload risk surfaced explicitly)."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("30000"),
        mca_stack=_mca_stack(
            active_mca_count=3,
            mca_monthly_load=Decimal("1000.00"),
            estimated_combined_holdback_pct=Decimal("40"),
        ),
    )
    assert offer is not None
    # Capacity: remaining 29000 / 0.15 = 193333 — way above base (50000)
    # and max (75000). Neither capped.
    # Overload: base 50000 x 0.75 = 37500 (already on 500 increment).
    # Max stays at 75000.
    assert offer.recommended_amount == Decimal("37500.00")
    assert offer.max_amount == Decimal("75000.00")
    assert offer.recommended_amount <= offer.max_amount  # invariant
    assert "discounted 25%" in offer.rationale
    assert "40" in offer.rationale  # the pct that fired
    assert str(int(HIGH_COMBINED_HOLDBACK_THRESHOLD)) in offer.rationale


def test_combined_holdback_exactly_at_35_pct_does_not_discount() -> None:
    """Strict ``>`` on the gate — exactly 35% does NOT fire."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("30000"),
        mca_stack=_mca_stack(
            active_mca_count=2,
            mca_monthly_load=Decimal("1000.00"),
            estimated_combined_holdback_pct=Decimal("35"),
        ),
    )
    assert offer is not None
    # No overload → recommended stays at 1.0x revenue.
    assert offer.recommended_amount == Decimal("50000.00")
    assert offer.max_amount == Decimal("75000.00")
    assert "discounted" not in offer.rationale


# ─────────────────────────────────────────────────────────────────────
# Case 4 — holdback capacity too tight, offer floors out
# ─────────────────────────────────────────────────────────────────────


def test_tight_capacity_floors_offer_to_none() -> None:
    """Existing $5,000/mo load against a $5,200/mo capacity → only
    $200/mo remaining. After capacity cap + rounding the offer lands
    below the $5,000 floor → None."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("5200"),
        mca_stack=_mca_stack(
            active_mca_count=4,
            mca_monthly_load=Decimal("5000.00"),
        ),
    )
    # remaining = 200; capacity_cap = 200/0.15 = 1333.33; rounds to 1500.
    # 1500 < MIN_OFFER_FLOOR (5000) → None.
    assert offer is None


def test_zero_remaining_capacity_returns_none() -> None:
    """Existing load equals capacity exactly → remaining = 0 → None
    short-circuit before any sizing math."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("5000"),
        mca_stack=_mca_stack(
            active_mca_count=4,
            mca_monthly_load=Decimal("5000.00"),
        ),
    )
    assert offer is None


# ─────────────────────────────────────────────────────────────────────
# Case 5 — result below 5000 returns None
# ─────────────────────────────────────────────────────────────────────


def test_small_revenue_below_floor_returns_none() -> None:
    """Tiny merchant whose 1.0x sizing lands at $3,000 — below the
    $5,000 floor → None."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("3000"),
        holdback_capacity_monthly=Decimal("1000"),
        mca_stack=_mca_stack(),
    )
    # No cap, no overload → recommended = 3000 < 5000 floor → None.
    assert offer is None
    assert MIN_OFFER_FLOOR == Decimal("5000")  # spec guard


# ─────────────────────────────────────────────────────────────────────
# Case 6 — rounding to nearest $500
# ─────────────────────────────────────────────────────────────────────


def test_rounding_to_nearest_500_half_up() -> None:
    """27250 → 27500 (54.5 x 500, half-up); 40875 → 41000 (81.75 x 500)."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("27250"),
        holdback_capacity_monthly=Decimal("1000000"),  # huge — no cap
        mca_stack=_mca_stack(),
    )
    assert offer is not None
    # recommended = 27250 (base) → 27500
    assert offer.recommended_amount == Decimal("27500.00")
    # max = 27250 * 1.5 = 40875 → 41000
    assert offer.max_amount == Decimal("41000.00")


def test_rounding_picks_lower_bucket_below_half() -> None:
    """27249.99 / 500 = 54.499... → 54 x 500 = 27000."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("27249.99"),
        holdback_capacity_monthly=Decimal("1000000"),
        mca_stack=_mca_stack(),
    )
    assert offer is not None
    assert offer.recommended_amount == Decimal("27000.00")


# ─────────────────────────────────────────────────────────────────────
# Structural guards
# ─────────────────────────────────────────────────────────────────────


def test_zero_revenue_returns_none() -> None:
    """No revenue, no offer."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("0"),
        holdback_capacity_monthly=Decimal("10000"),
        mca_stack=_mca_stack(),
    )
    assert offer is None


def test_negative_capacity_returns_none() -> None:
    """Operator-entered negative capacity is treated as no capacity."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("-1"),
        mca_stack=_mca_stack(),
    )
    assert offer is None


def test_holdback_pct_is_always_default() -> None:
    """``holdback_pct`` is the constant 15% — surfaced for the Close
    sync. Never tuned per merchant (v1)."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("12500"),
        mca_stack=_mca_stack(),
    )
    assert offer is not None
    assert offer.holdback_pct == Decimal("0.15")
    assert offer.holdback_pct == DEFAULT_HOLDBACK_PCT


def test_max_amount_never_below_recommended() -> None:
    """Invariant. Even with the overload discount applied to base,
    max remains at the capacity cap so max >= recommended always."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("30000"),
        mca_stack=_mca_stack(
            active_mca_count=3,
            mca_monthly_load=Decimal("1000.00"),
            estimated_combined_holdback_pct=Decimal("60"),
        ),
    )
    assert offer is not None
    assert offer.max_amount >= offer.recommended_amount


def test_model_has_no_decline_or_score_field() -> None:
    """Mirror the Track A / B / C + mca_stack + balance_health guard."""
    fields = set(OfferRecommendation.model_fields)
    forbidden = {
        "decline",
        "auto_decline",
        "risk_score",
        "fraud_score",
        "score",
        "outcome",
        "hard_decline_reasons",
    }
    leaked = fields & forbidden
    assert not leaked, f"OfferRecommendation must not carry decline/score fields; leaked: {leaked}"
