"""PA A9 — funder ``supported_products`` filter inside ``match_funder``.

Migration 081 added ``funders.supported_products`` so the matcher can
narrow the eligible funder set to the funders that actually write a
given product. The filter runs BEFORE criteria evaluation so a funder
that can't write the merchant's product never surfaces on the dossier,
regardless of how well the merchant scores against its underwriting
gates.

Backwards-compatibility: empty ``supported_products`` tuple means "no
constraint" — the funder writes every product. Existing rows in prod
default to NULL → empty tuple via the repo mapper, so no funder is
accidentally excluded.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal, cast
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _deal(**overrides: object) -> ScoreInput:
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Restaurant",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="722511",
        industry_choice="Restaurant / Food Service",
        time_in_business_months=36,
        credit_score=720,
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
    return base.model_copy(update=overrides)


def _score(*, tier: Literal["A", "B", "C", "D", "F"] = "B") -> ScoreResult:
    return ScoreResult(
        score=75,
        tier=tier,
        recommendation="approve",
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def _funder(**overrides: object) -> FunderRow:
    base = FunderRow(name="Test Funder", min_credit_score=600)
    return base.model_copy(update=overrides)


def _merchant_with_product(product_type: str | None) -> MerchantRow:
    """Build a MerchantRow with ``product_type`` attached via duck-type.

    PA A7 (parallel agent, not yet merged) wires ``product_type`` onto
    ``MerchantRow``. Until that lands, the field doesn't exist on the
    Pydantic model so we use ``SimpleNamespace`` to mimic the shape
    ``match_funder`` reads (it only calls ``getattr(merchant,
    "product_type", None)`` and ``merchant.web_presence_flags`` /
    ``merchant.ucc_filings`` / ``merchant.ucc_default_indicators``).

    Cast satisfies mypy without forcing a model edit that conflicts
    with PA A7's territory.
    """
    ns = SimpleNamespace(
        id=uuid4(),
        product_type=product_type,
        web_presence_flags=None,
        ucc_filings=None,
        ucc_default_indicators=None,
    )
    return cast(MerchantRow, ns)


# ─────────────────────────────────────────────────────────────────────
# 1. Funder with non-matching supported_products → excluded
# ─────────────────────────────────────────────────────────────────────


def test_funder_excluded_when_supported_products_does_not_include_merchant_product() -> None:
    funder = _funder(deal_types_accepted=("mca",))
    deal = _deal()
    merchant = _merchant_with_product("business_loan")
    result = match_funder(funder, deal, _score(), merchant=merchant)
    assert result is None


# ─────────────────────────────────────────────────────────────────────
# 2. Funder with matching supported_products → included (criteria eval)
# ─────────────────────────────────────────────────────────────────────


def test_funder_included_when_supported_products_contains_merchant_product() -> None:
    funder = _funder(deal_types_accepted=("term_loan",))
    deal = _deal()
    merchant = _merchant_with_product("business_loan")
    result = match_funder(funder, deal, _score(), merchant=merchant)
    assert result is not None
    assert result.funder_name == "Test Funder"


# ─────────────────────────────────────────────────────────────────────
# 3. Legacy funder with empty supported_products → matches every product
# ─────────────────────────────────────────────────────────────────────


def test_funder_with_empty_supported_products_matches_every_product() -> None:
    """The migration-081 default: NULL → empty tuple → no constraint.
    Every existing funder in prod sits here; flipping the filter live
    must not silently exclude them."""
    funder = _funder()  # deal_types_accepted defaults to ()
    deal = _deal()
    for product in (
        "revenue_based",
        "business_loan",
        "line_of_credit",
        "equipment",
        "asset_based",
        "receivables",
    ):
        merchant = _merchant_with_product(product)
        result = match_funder(funder, deal, _score(), merchant=merchant)
        assert result is not None, f"empty supported_products excluded {product!r}"


# ─────────────────────────────────────────────────────────────────────
# 4. Merchant with no product_type set / None → defaults to revenue_based
# ─────────────────────────────────────────────────────────────────────


def test_funder_with_revenue_based_filter_includes_legacy_merchant() -> None:
    """Legacy pre-PA-A7 merchants have no ``product_type`` field;
    ``getattr`` returns None and ``coerce_product_type`` lands on
    ``revenue_based``. A revenue-based funder must still match them."""
    funder = _funder(deal_types_accepted=("mca",))
    deal = _deal()
    merchant = _merchant_with_product(None)
    result = match_funder(funder, deal, _score(), merchant=merchant)
    assert result is not None


def test_legacy_merchant_no_explicit_product_type_skips_new_filter() -> None:
    """Legacy merchant (``product_type=None``) opts OUT of the new
    early-exit filter — only explicit product_type triggers it. With
    the funder having no deal_types_accepted constraint at all, the
    legacy merchant matches normally (the new filter never fires)."""
    funder = _funder()  # no deal_types_accepted constraint
    deal = _deal()
    merchant = _merchant_with_product(None)
    result = match_funder(funder, deal, _score(), merchant=merchant)
    assert result is not None, "legacy merchant excluded by new filter (regression)"


# ─────────────────────────────────────────────────────────────────────
# 5. Mixed funder catalog — filter narrows correctly
# ─────────────────────────────────────────────────────────────────────


def test_mixed_funder_set_only_product_eligible_funders_score() -> None:
    """Three funders: revenue-only / business-loan-only / no-constraint.
    For a business_loan merchant, the first is excluded, the other two
    are scored."""
    revenue_funder = _funder(name="RevenueOnly Funder", deal_types_accepted=("mca",))
    loan_funder = _funder(name="LoanOnly Funder", deal_types_accepted=("term_loan",))
    legacy_funder = _funder(name="Legacy AllProducts Funder")
    deal = _deal()
    merchant = _merchant_with_product("business_loan")
    score = _score()

    assert match_funder(revenue_funder, deal, score, merchant=merchant) is None
    loan_result = match_funder(loan_funder, deal, score, merchant=merchant)
    assert loan_result is not None
    assert loan_result.funder_name == "LoanOnly Funder"
    legacy_result = match_funder(legacy_funder, deal, score, merchant=merchant)
    assert legacy_result is not None
    assert legacy_result.funder_name == "Legacy AllProducts Funder"


# ─────────────────────────────────────────────────────────────────────
# 6. Regression — no merchant kwarg still works (legacy callers)
# ─────────────────────────────────────────────────────────────────────


def test_match_funder_without_merchant_kwarg_still_applies_product_filter() -> None:
    """Some callers pass merchant=None (legacy /api/deals/{id}/score
    path). The filter must still run — getattr(None, ...) defaults to
    revenue_based, and a revenue-based funder must include None-merchant
    deals (i.e. no behavior change vs. pre-PA-A9)."""
    funder = _funder(deal_types_accepted=("mca",))
    result = match_funder(funder, _deal(), _score())
    assert result is not None


def test_match_funder_without_merchant_kwarg_skips_new_product_filter() -> None:
    """No merchant kwarg → no explicit product_type → new filter skips
    entirely (legacy backwards-compat). With no deal_types_accepted
    constraint, the legacy path returns a match unchanged."""
    funder = _funder()  # no deal_types_accepted constraint
    result = match_funder(funder, _deal(), _score())
    assert result is not None, "no-merchant call excluded by new filter (regression)"
