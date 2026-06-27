"""Tests for product-aware ``compute_offer`` dispatch (Phase A Agent 8).

Covers the 5 non-revenue product paths + a regression guard for the
revenue_based path. The revenue_based path's exhaustive behavior is
covered by ``tests/scoring_v2/test_offer.py`` — this file only asserts
that the dispatched call hits the same code (i.e., adding the
``product_type`` kwarg doesn't break the historical path).

AEGIS rule: Decimal-only money math. Every assertion below uses
``Decimal`` literals — never float — and ``assert x == Decimal("…")``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aegis.product_types import DEFAULT_PRODUCT_TYPE
from aegis.scoring_v2.mca_stack import MCAStackAggregation
from aegis.scoring_v2.offer import (
    ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER,
    BUSINESS_LOAN_APR_PLACEHOLDER,
    BUSINESS_LOAN_REVENUE_MULTIPLE,
    BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER,
    EQUIPMENT_APR_PLACEHOLDER,
    EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER,
    EQUIPMENT_TERM_MONTHS_PLACEHOLDER,
    LOC_APR_PLACEHOLDER,
    LOC_REVENUE_MULTIPLE,
    RECEIVABLES_ADVANCE_RATE_PCT_DEFAULT,
    RECEIVABLES_FACTORING_FEE_PCT_PER_30D_DEFAULT,
    RECEIVABLES_RESERVE_PCT_DEFAULT,
    OfferRecommendation,
    _pmt,
    compute_offer,
)


def _empty_stack() -> MCAStackAggregation:
    """Empty MCA stack — no positions, no monthly load. Used by every
    non-revenue product path since the stack is ignored for those."""
    return MCAStackAggregation(
        active_mca_count=0,
        active_mca_source_ids=(),
        mca_monthly_load=Decimal("0.00"),
        mca_monthly_load_source_ids=(),
        estimated_combined_holdback_pct=None,
        largest_single_mca_monthly=Decimal("0.00"),
        largest_single_mca_lender=None,
        largest_single_mca_source_ids=(),
        shadow_triggers=(),
    )


# ---------------------------------------------------------------------
# revenue_based regression guards
# ---------------------------------------------------------------------


def test_revenue_based_default_path_unchanged() -> None:
    """No product_type kwarg → revenue_based behavior, byte-identical
    to the prior signature. Existing call sites that don't pass
    product_type must continue to work."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("100000"),
        holdback_capacity_monthly=Decimal("25000"),
        mca_stack=_empty_stack(),
    )
    assert offer is not None
    assert offer.product_type == "revenue_based"
    # 1.0x revenue, no capacity cap, no overload — recommended = $100k
    assert offer.recommended_amount == Decimal("100000.00")
    # 1.5x revenue ceiling
    assert offer.max_amount == Decimal("150000.00")
    assert offer.holdback_pct == Decimal("0.15")
    # Product-specific fields stay None
    assert offer.loan_amount is None
    assert offer.credit_limit is None
    assert offer.financed_amount is None


def test_revenue_based_explicit_product_type() -> None:
    """Explicit ``product_type="revenue_based"`` matches default-kwarg
    behavior. Same outputs as the implicit path above."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("100000"),
        holdback_capacity_monthly=Decimal("25000"),
        mca_stack=_empty_stack(),
        product_type="revenue_based",
    )
    assert offer is not None
    assert offer.product_type == "revenue_based"
    assert offer.recommended_amount == Decimal("100000.00")
    assert offer.max_amount == Decimal("150000.00")


def test_default_product_type_constant_is_revenue_based() -> None:
    """Tripwire: the module-level default literal must match what the
    compute_offer signature defaults to. Catches divergence if either
    side is edited without the other."""
    assert DEFAULT_PRODUCT_TYPE == "revenue_based"


# ---------------------------------------------------------------------
# business_loan
# ---------------------------------------------------------------------


def test_business_loan_sized_at_3x_monthly_revenue() -> None:
    monthly_revenue = Decimal("50000")
    offer = compute_offer(
        true_revenue_monthly=monthly_revenue,
        holdback_capacity_monthly=Decimal("0"),  # ignored
        mca_stack=_empty_stack(),
        product_type="business_loan",
    )
    assert offer is not None
    assert offer.product_type == "business_loan"
    # 3x $50k = $150k, rounded to nearest $500
    expected_loan = monthly_revenue * BUSINESS_LOAN_REVENUE_MULTIPLE
    assert offer.loan_amount == expected_loan.quantize(Decimal("0.01"))
    assert offer.recommended_amount == offer.loan_amount
    assert offer.max_amount == offer.loan_amount
    assert offer.interest_rate_apr == BUSINESS_LOAN_APR_PLACEHOLDER
    assert offer.term_months == BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER
    assert offer.monthly_payment is not None
    # total_cost = monthly_payment * term
    assert offer.total_cost == (
        offer.monthly_payment * Decimal(BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER)
    ).quantize(Decimal("0.01"))
    assert "PLACEHOLDER" in offer.rationale


def test_business_loan_zero_revenue_returns_none() -> None:
    """Term loan needs monthly revenue to size — zero / negative → None."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("0"),
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="business_loan",
    )
    assert offer is None


def test_business_loan_ignores_unrelated_kwargs() -> None:
    """``equipment_cost`` in kwargs on a business_loan call → ignored
    silently (wrong product, no effect)."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="business_loan",
        equipment_cost=Decimal("999999"),  # ignored
    )
    assert offer is not None
    # Still sized off revenue, not the irrelevant equipment_cost
    assert offer.loan_amount == (Decimal("50000") * BUSINESS_LOAN_REVENUE_MULTIPLE).quantize(
        Decimal("0.01")
    )


# ---------------------------------------------------------------------
# line_of_credit
# ---------------------------------------------------------------------


def test_line_of_credit_sized_at_1_5x_monthly_revenue() -> None:
    monthly_revenue = Decimal("40000")
    offer = compute_offer(
        true_revenue_monthly=monthly_revenue,
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="line_of_credit",
    )
    assert offer is not None
    assert offer.product_type == "line_of_credit"
    expected_limit = (monthly_revenue * LOC_REVENUE_MULTIPLE).quantize(Decimal("0.01"))
    assert offer.credit_limit == expected_limit
    assert offer.recommended_amount == expected_limit
    assert offer.interest_rate_apr == LOC_APR_PLACEHOLDER
    assert offer.draw_rate == Decimal("0.0")
    assert offer.holdback_pct == Decimal("0.0")
    assert offer.loan_amount is None


# ---------------------------------------------------------------------
# equipment
# ---------------------------------------------------------------------


def test_equipment_sized_off_supplied_equipment_cost() -> None:
    equipment_cost = Decimal("50000")
    offer = compute_offer(
        true_revenue_monthly=Decimal("30000"),
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="equipment",
        equipment_cost=equipment_cost,
    )
    assert offer is not None
    assert offer.product_type == "equipment"
    # $50k * (1 - 0.10) = $45k, rounded
    expected_financed = (
        equipment_cost * (Decimal(1) - EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER)
    ).quantize(Decimal("0.01"))
    assert offer.financed_amount == expected_financed
    assert offer.recommended_amount == expected_financed
    assert offer.down_payment_pct == EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER
    assert offer.interest_rate_apr == EQUIPMENT_APR_PLACEHOLDER
    assert offer.term_months == EQUIPMENT_TERM_MONTHS_PLACEHOLDER
    assert offer.monthly_payment is not None
    assert offer.total_cost == (
        offer.monthly_payment * Decimal(EQUIPMENT_TERM_MONTHS_PLACEHOLDER)
    ).quantize(Decimal("0.01"))


def test_equipment_without_equipment_cost_raises() -> None:
    """Operator must supply ``equipment_cost`` for this product — no
    sensible default."""
    with pytest.raises(ValueError, match="equipment_cost"):
        compute_offer(
            true_revenue_monthly=Decimal("30000"),
            holdback_capacity_monthly=Decimal("0"),
            mca_stack=_empty_stack(),
            product_type="equipment",
        )


# ---------------------------------------------------------------------
# asset_based
# ---------------------------------------------------------------------


def test_asset_based_sized_off_eligible_collateral() -> None:
    collateral = Decimal("100000")
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="asset_based",
        eligible_collateral=collateral,
    )
    assert offer is not None
    assert offer.product_type == "asset_based"
    expected_limit = (collateral * ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER).quantize(
        Decimal("0.01")
    )
    assert offer.revolving_limit == expected_limit
    assert offer.recommended_amount == expected_limit
    assert offer.advance_rate_pct == ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER
    assert offer.eligible_collateral_estimate == collateral.quantize(Decimal("0.01"))


def test_asset_based_without_eligible_collateral_raises() -> None:
    with pytest.raises(ValueError, match="eligible_collateral"):
        compute_offer(
            true_revenue_monthly=Decimal("50000"),
            holdback_capacity_monthly=Decimal("0"),
            mca_stack=_empty_stack(),
            product_type="asset_based",
        )


# ---------------------------------------------------------------------
# receivables
# ---------------------------------------------------------------------


def test_receivables_default_advance_reserve_fee() -> None:
    offer = compute_offer(
        true_revenue_monthly=Decimal("40000"),
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="receivables",
    )
    assert offer is not None
    assert offer.product_type == "receivables"
    assert offer.advance_rate_pct == RECEIVABLES_ADVANCE_RATE_PCT_DEFAULT
    assert offer.reserve_pct == RECEIVABLES_RESERVE_PCT_DEFAULT
    assert offer.factoring_fee_pct == RECEIVABLES_FACTORING_FEE_PCT_PER_30D_DEFAULT
    # No invoice supplied → sized off revenue at 80% advance
    expected = (Decimal("40000") * RECEIVABLES_ADVANCE_RATE_PCT_DEFAULT).quantize(Decimal("0.01"))
    assert offer.recommended_amount == expected


def test_receivables_with_explicit_invoice_face_value() -> None:
    offer = compute_offer(
        true_revenue_monthly=Decimal("40000"),
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="receivables",
        invoice_face_value=Decimal("75000"),
    )
    assert offer is not None
    # 75k x 80% = 60k
    expected = (Decimal("75000") * RECEIVABLES_ADVANCE_RATE_PCT_DEFAULT).quantize(Decimal("0.01"))
    assert offer.recommended_amount == expected


# ---------------------------------------------------------------------
# Decimal-precision invariant
# ---------------------------------------------------------------------


def test_every_monetary_output_is_decimal_not_float() -> None:
    """All monetary outputs must be Decimal across all product types.
    Catches any accidental float intermediate that survived rounding."""
    revenue = Decimal("60000")
    cases: list[tuple[str, dict[str, Decimal]]] = [
        ("revenue_based", {}),
        ("business_loan", {}),
        ("line_of_credit", {}),
        ("equipment", {"equipment_cost": Decimal("80000")}),
        ("asset_based", {"eligible_collateral": Decimal("120000")}),
        ("receivables", {}),
    ]
    for product_type, kwargs in cases:
        offer = compute_offer(
            true_revenue_monthly=revenue,
            holdback_capacity_monthly=Decimal("20000"),
            mca_stack=_empty_stack(),
            product_type=product_type,  # type: ignore[arg-type]
            **kwargs,
        )
        assert offer is not None, f"{product_type} returned None unexpectedly"
        for field_name in (
            "recommended_amount",
            "max_amount",
            "holdback_pct",
            "loan_amount",
            "monthly_payment",
            "total_cost",
            "credit_limit",
            "financed_amount",
            "revolving_limit",
            "eligible_collateral_estimate",
        ):
            value = getattr(offer, field_name)
            if value is not None:
                assert isinstance(value, Decimal), (
                    f"{product_type}.{field_name} = {value!r} (type {type(value).__name__}); "
                    f"expected Decimal"
                )


# ---------------------------------------------------------------------
# PMT helper
# ---------------------------------------------------------------------


def test_pmt_zero_rate_returns_even_split() -> None:
    assert _pmt(Decimal("0"), 10, Decimal("1000")) == Decimal("100.00")


def test_pmt_known_input_matches_closed_form() -> None:
    """100k principal, 12% APR, 60 months → ~$2224.44/mo by standard
    amortisation. Use a tolerance of 1 cent on Decimal output."""
    rate_monthly = Decimal("0.12") / Decimal(12)
    pmt = _pmt(rate_monthly, 60, Decimal("100000"))
    expected = Decimal("2224.44")
    assert abs(pmt - expected) < Decimal("0.01"), f"got {pmt}, expected ~{expected}"


def test_offer_recommendation_round_trips_through_pydantic() -> None:
    """Strict-extra config doesn't trip on the new optional fields when
    they're populated for a non-revenue product."""
    offer = compute_offer(
        true_revenue_monthly=Decimal("50000"),
        holdback_capacity_monthly=Decimal("0"),
        mca_stack=_empty_stack(),
        product_type="business_loan",
    )
    assert offer is not None
    dumped = offer.model_dump()
    rebuilt = OfferRecommendation.model_validate(dumped)
    assert rebuilt == offer
