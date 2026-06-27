"""Offer sizing — first-class output for the dossier "suggested offer" chip.

Derives a recommended advance amount and ceiling for a merchant from
three deterministic inputs: monthly true revenue, monthly holdback
capacity (the merchant's operator-confirmed sustainable debt service
load), and the existing MCA-stack rollup. Pure function over those
inputs — no LLM, no DB, no audit-log side effects.

Product-aware sizing (Phase A Agent 8)
-------------------------------------
``compute_offer`` accepts an optional ``product_type`` kwarg that
dispatches to one of six product-specific sizing paths. The legacy
``revenue_based`` path is the default and its behavior is unchanged
byte-for-byte from prior versions. The other five products use simple
heuristics for now — see ``_PRODUCT_PLACEHOLDER_RATIONALE`` constants
below — clearly labelled as such per operating-principle 4 (no
industry-typical placeholders presented as Commera rates).

Product-specific outputs are surfaced on additional optional fields on
``OfferRecommendation``. The required core fields
(``recommended_amount`` / ``max_amount`` / ``holdback_pct`` /
``rationale``) are populated with product-equivalent values for every
product so existing call sites + templates don't break.

* ``recommended_amount`` — sized at ``true_revenue_monthly * 1.0``,
  then capped by remaining holdback capacity, then knocked down 25%
  when the merchant's existing combined holdback already exceeds 35%
  (revenue_based only). For other products: equivalent product-sized
  principal.
* ``max_amount`` — sized at ``true_revenue_monthly * 1.5``, then
  capped by the same remaining-capacity ceiling. The 25% overload
  discount intentionally does NOT apply — ``max_amount`` represents
  the aggressive cap an underwriter could stretch to if they accept
  the overload risk; ``recommended_amount`` is the cautious figure
  (revenue_based only). Other products use the same principal for
  recommended + max (no separate ceiling).
* ``holdback_pct`` — the default 15% advance-to-monthly-payment rate
  the capacity check is computed against. Surfaced as a return field
  so the chip + the Close-side "Recommended Holdback Pct" custom
  field both read from one source.
* ``rationale`` — one-line human-readable explanation naming the
  limiting factor. The underwriter sees WHY the offer landed where
  it did without having to re-derive.

Both amounts round to the nearest $500 increment (MCA underwriting
convention). If the rounded ``recommended_amount`` falls below
$5,000 — the operator floor — the function returns ``None``: the
deal is too small to size and the dossier suppresses the chip
entirely rather than rendering a meaningless tiny number. Floor
applies to revenue_based only; other products defer to the operator-
supplied principal (equipment cost, collateral, etc.).

DECISION-BOUNDARY POSTURE — SHADOW ONLY
---------------------------------------
The whole module is operator-informational. Offer sizing does NOT
affect ``aegis.scoring.score``'s decline path — the live decline
gates (``MAX_DEBT_TO_REVENUE``, ``MCA_POSITIONS_HARD_DECLINE``,
``DAYS_NEGATIVE_HARD_DECLINE``, etc.) continue to govern, and the
existing ``score_result.suggested_max_advance`` continues to feed
the funder-match grid. Per CLAUDE.md "Decision-boundary changes —
shadow-first": this surface adds an informational chip; any future
flip to consume it in the decline path would be a config / env-var
change, not a code deploy. The product-aware extension preserves
this posture — no new product moves any doc from proceed to
manual_review.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money
from aegis.product_types import DEFAULT_PRODUCT_TYPE, ProductType
from aegis.scoring_v2.mca_stack import MCAStackAggregation

BASE_REVENUE_MULTIPLE: Final[Decimal] = Decimal("1.0")
"""``recommended_amount`` starts at 1.0x monthly true revenue."""

MAX_REVENUE_MULTIPLE: Final[Decimal] = Decimal("1.5")
"""``max_amount`` starts at 1.5x monthly true revenue — the aggressive
ceiling an underwriter could stretch to."""

DEFAULT_HOLDBACK_PCT: Final[Decimal] = Decimal("0.15")
"""15% monthly-payment-to-advance ratio used for the capacity check
and surfaced as ``holdback_pct``. ``advance * 0.15`` is the implied
monthly payment under this rate."""

HIGH_COMBINED_HOLDBACK_THRESHOLD: Final[Decimal] = Decimal("35")
"""When the merchant's existing ``estimated_combined_holdback_pct``
exceeds this value (strict ``>``), discount ``recommended_amount``
by ``HIGH_HOLDBACK_DISCOUNT_FACTOR``."""

HIGH_HOLDBACK_DISCOUNT_FACTOR: Final[Decimal] = Decimal("0.75")
"""Multiplier applied to ``recommended_amount`` when the merchant's
existing combined holdback is overloaded. ``0.75`` == 25% knock-down."""

MIN_OFFER_FLOOR: Final[Decimal] = Decimal("5000")
"""``recommended_amount`` below this value (after rounding) returns
``None`` — too small to size meaningfully. Applies to ``revenue_based``
only; other product paths defer to operator-supplied principal."""

ROUNDING_INCREMENT: Final[Decimal] = Decimal("500")
"""Both amounts round to the nearest multiple of this value
(half-up). MCA underwriting convention."""

_RATIONALE_MAX_LENGTH: Final[int] = 320

# --- Product placeholder constants ------------------------------------
# ALL VALUES BELOW ARE INDUSTRY-TYPICAL PLACEHOLDERS pending operator
# calibration. They are NOT Commera-published rates. Per
# operating-principle 4 ("funder seeding sub-rule"): "Industry-typical
# placeholders look right, match nothing, and surface as bogus match
# results to the underwriter." These exist only so the offer chip can
# render *something* for the non-revenue-based products until the
# operator wires real product-specific rate tables.

BUSINESS_LOAN_REVENUE_MULTIPLE: Final[Decimal] = Decimal("3.0")
"""PLACEHOLDER: term-loan principal sized at 3x monthly true revenue."""

BUSINESS_LOAN_APR_PLACEHOLDER: Final[Decimal] = Decimal("0.16")
"""PLACEHOLDER: 16% APR fixed. Real rate depends on grade + amount."""

BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER: Final[int] = 12
"""PLACEHOLDER: 12-month term. Real term depends on amount + grade."""

LOC_REVENUE_MULTIPLE: Final[Decimal] = Decimal("1.5")
"""PLACEHOLDER: LOC credit limit sized at 1.5x monthly true revenue."""

LOC_APR_PLACEHOLDER: Final[Decimal] = Decimal("0.18")
"""PLACEHOLDER: 18% APR. Real rate depends on draw + utilization."""

EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER: Final[Decimal] = Decimal("0.10")
"""PLACEHOLDER: 10% down. Real % depends on equipment age + class."""

EQUIPMENT_APR_PLACEHOLDER: Final[Decimal] = Decimal("0.12")
"""PLACEHOLDER: 12% APR. Real rate depends on equipment + credit."""

EQUIPMENT_TERM_MONTHS_PLACEHOLDER: Final[int] = 60
"""PLACEHOLDER: 60-month term. Real term tracks equipment useful life."""

ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER: Final[Decimal] = Decimal("0.80")
"""PLACEHOLDER: 80% advance against eligible collateral."""

RECEIVABLES_ADVANCE_RATE_PCT_DEFAULT: Final[Decimal] = Decimal("0.80")
"""DEFAULT: 80% advance is the factoring industry norm. Operator
overrides per debtor concentration."""

RECEIVABLES_RESERVE_PCT_DEFAULT: Final[Decimal] = Decimal("0.10")
"""DEFAULT: 10% reserve held until invoice clears."""

RECEIVABLES_FACTORING_FEE_PCT_PER_30D_DEFAULT: Final[Decimal] = Decimal("0.03")
"""DEFAULT: 3% per 30 days. Real fee depends on debtor quality."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class OfferRecommendation(_StrictModel):
    """Sized offer for the dossier chip + the Close opportunity-side
    sync. Always carries a recommended-vs-max pair plus the limiting
    factor's rationale so the underwriter doesn't have to re-derive.

    The function returns ``None`` rather than an instance with a
    sub-floor amount — callers therefore know that a non-None value
    represents an offer worth surfacing.

    Product-specific optional fields are populated when ``product_type``
    is not ``revenue_based``. Templates branch on ``product_type`` to
    decide which optional fields to render.
    """

    # --- Core fields (populated for every product) ---
    product_type: ProductType = Field(
        default=DEFAULT_PRODUCT_TYPE,
        description=(
            "Discriminator. Templates branch on this to render the "
            "product-appropriate offer chip. Defaults to revenue_based "
            "to preserve current behavior for callers that haven't been "
            "wired through with merchant.product_type yet."
        ),
    )
    recommended_amount: Money = Field(
        description=(
            "Cautious advance recommendation (revenue_based) OR the "
            "product-equivalent principal (loan amount, credit limit, "
            "financed amount, revolving limit, expected advance). "
            "Rounded to the nearest $500."
        ),
    )
    max_amount: Money = Field(
        description=(
            "Aggressive ceiling (revenue_based — 1.5x monthly revenue, "
            "capped by holdback capacity). For non-revenue products, "
            "equals ``recommended_amount`` (no separate ceiling). "
            "Rounded to the nearest $500."
        ),
    )
    holdback_pct: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("1"),
        description=(
            "Monthly-payment-to-advance ratio assumed when sizing the "
            "offer. ``0.15`` for revenue_based. For non-revenue products, "
            "implied monthly_payment / monthly_revenue when revenue is "
            "known, else 0."
        ),
    )
    rationale: str = Field(
        max_length=_RATIONALE_MAX_LENGTH,
        description=(
            "One-line human-readable explanation naming the limiting "
            "factor — capacity cap, overload discount, plain revenue "
            "multiple, or the product-specific sizing source. Read "
            "directly into the chip tooltip."
        ),
    )

    # --- business_loan fields (None for other products) ---
    loan_amount: Money | None = Field(
        default=None,
        description="business_loan: principal. = recommended_amount.",
    )
    interest_rate_apr: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description=(
            "business_loan / line_of_credit / equipment: stated APR. "
            "PLACEHOLDER values until operator calibrates per product."
        ),
    )
    term_months: int | None = Field(
        default=None,
        ge=1,
        description="business_loan / equipment: amortisation term.",
    )
    monthly_payment: Money | None = Field(
        default=None,
        description=("business_loan / equipment: amortised monthly payment via Decimal PMT."),
    )
    total_cost: Money | None = Field(
        default=None,
        description="business_loan / equipment: monthly_payment x term_months.",
    )

    # --- line_of_credit fields ---
    credit_limit: Money | None = Field(
        default=None,
        description="line_of_credit: revolving credit limit. = recommended_amount.",
    )
    draw_rate: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description=(
            "line_of_credit: rate applied per draw. Defaults to 0 — "
            "operator sets per draw at submission time."
        ),
    )

    # --- equipment fields ---
    financed_amount: Money | None = Field(
        default=None,
        description=("equipment: principal financed (after down payment). = recommended_amount."),
    )
    down_payment_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description="equipment: down payment as % of equipment cost.",
    )

    # --- asset_based fields ---
    revolving_limit: Money | None = Field(
        default=None,
        description="asset_based: revolver limit. = recommended_amount.",
    )
    advance_rate_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description=(
            "asset_based / receivables: advance against eligible "
            "collateral (asset_based) or face value of invoice "
            "(receivables)."
        ),
    )
    eligible_collateral_estimate: Money | None = Field(
        default=None,
        description=(
            "asset_based: operator-supplied or merchant-context-derived "
            "eligible collateral total used to size revolving_limit."
        ),
    )

    # --- receivables fields ---
    reserve_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description="receivables: % of face held back until invoice clears.",
    )
    factoring_fee_pct: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description="receivables: fee per 30-day aging bucket.",
    )


def _pmt(rate_monthly: Decimal, n_months: int, principal: Decimal) -> Decimal:
    """Amortised monthly payment using pure-Decimal PMT formula.

    Pure Decimal — no float intermediates. Per AEGIS rule "NEVER use
    ``float`` for money". For zero-rate principals returns the simple
    even split. Per AEGIS "NEVER hand-roll numerics" we keep this to
    the algebraic closed form of PMT — no iteration, no root finding;
    if rate calibration ever needs APR-from-cashflow inversion that's
    where ``scipy.optimize.brentq`` comes in.

    PMT = P * (r * (1+r)^n) / ((1+r)^n - 1)
    """
    if rate_monthly == 0:
        return (principal / Decimal(n_months)).quantize(Decimal("0.01"))
    factor = (Decimal(1) + rate_monthly) ** n_months
    payment = principal * (rate_monthly * factor) / (factor - Decimal(1))
    return payment.quantize(Decimal("0.01"))


def compute_offer(
    true_revenue_monthly: Decimal,
    holdback_capacity_monthly: Decimal,
    mca_stack: MCAStackAggregation,
    *,
    product_type: ProductType = DEFAULT_PRODUCT_TYPE,
    # ``Any`` here is the right type — ``product_kwargs`` accepts
    # operator-supplied product-specific inputs whose key/value shapes
    # differ per product (``equipment_cost: Decimal`` vs
    # ``eligible_collateral: Decimal`` vs ``invoice_face_value: Decimal``).
    # Per-product validation happens inside the dispatch branches.
    **product_kwargs: Any,  # noqa: ANN401 — see justification comment above
) -> OfferRecommendation | None:
    """Size a recommended + max offer for the merchant.

    Parameters
    ----------
    true_revenue_monthly
        ``parser.aggregate._true_revenue`` projected to a 30-day month
        (i.e. ``AnalysisRow.monthly_revenue``). Zero / negative produces
        ``None`` for ``revenue_based`` — no revenue, no offer. Other
        product paths use ``true_revenue_monthly`` for monthly-payment
        coverage rationale strings; zero revenue still returns an offer
        for operator-supplied principals (equipment / asset_based).
    holdback_capacity_monthly
        Operator-confirmed monthly debt-service budget. Anchors the
        capacity cap for ``revenue_based`` together with
        ``mca_stack.mca_monthly_load``. Ignored for non-revenue products
        (those use product-specific sizing inputs from ``product_kwargs``).
    mca_stack
        Existing MCA stack rollup. Read for ``mca_monthly_load``
        (capacity check) and ``estimated_combined_holdback_pct``
        (overload discount trigger) by ``revenue_based`` only.
    product_type
        Discriminator (default ``"revenue_based"``). Dispatches to
        product-specific sizing.
    **product_kwargs
        Product-specific inputs:

        * ``equipment``: ``equipment_cost: Decimal`` (REQUIRED).
        * ``asset_based``: ``eligible_collateral: Decimal`` (REQUIRED).
        * ``receivables``: optional overrides for ``advance_rate_pct``
          / ``reserve_pct`` / ``factoring_fee_pct``.
        * Other products: ignored.

    Returns
    -------
    OfferRecommendation | None
        ``None`` for any of:

        * ``revenue_based`` + ``true_revenue_monthly <= 0``
        * ``revenue_based`` + remaining holdback capacity ``<= 0``
        * ``revenue_based`` + ``recommended_amount`` < ``$5,000``
          after rounding (deal too small to size).
        * ``business_loan`` / ``line_of_credit`` + zero monthly
          revenue (can't size off revenue).

        Otherwise a populated ``OfferRecommendation``.

    Raises
    ------
    ValueError
        ``equipment`` without ``equipment_cost`` in kwargs, or
        ``asset_based`` without ``eligible_collateral``. Operator must
        supply for these product paths.
    """
    if product_type == "revenue_based":
        return _compute_offer_revenue_based(
            true_revenue_monthly=true_revenue_monthly,
            holdback_capacity_monthly=holdback_capacity_monthly,
            mca_stack=mca_stack,
        )
    if product_type == "business_loan":
        return _compute_offer_business_loan(true_revenue_monthly=true_revenue_monthly)
    if product_type == "line_of_credit":
        return _compute_offer_line_of_credit(true_revenue_monthly=true_revenue_monthly)
    if product_type == "equipment":
        return _compute_offer_equipment(
            true_revenue_monthly=true_revenue_monthly,
            product_kwargs=product_kwargs,
        )
    if product_type == "asset_based":
        return _compute_offer_asset_based(
            true_revenue_monthly=true_revenue_monthly,
            product_kwargs=product_kwargs,
        )
    if product_type == "receivables":
        return _compute_offer_receivables(
            true_revenue_monthly=true_revenue_monthly,
            product_kwargs=product_kwargs,
        )
    # Unreachable in well-typed callers, but defensive: mypy + Pydantic
    # narrow this on the Literal so the fallback never fires; if a new
    # product_type lands without updating dispatch, return None rather
    # than fabricate an offer.
    return None  # pragma: no cover


def _compute_offer_revenue_based(
    *,
    true_revenue_monthly: Decimal,
    holdback_capacity_monthly: Decimal,
    mca_stack: MCAStackAggregation,
) -> OfferRecommendation | None:
    """Legacy revenue-based / MCA sizing. Byte-for-byte the prior
    ``compute_offer`` behavior — extracted to a helper so the public
    function can dispatch on product_type without duplicating logic."""
    if true_revenue_monthly <= 0:
        return None

    remaining_capacity = holdback_capacity_monthly - mca_stack.mca_monthly_load
    if remaining_capacity <= 0:
        return None

    base_amount = true_revenue_monthly * BASE_REVENUE_MULTIPLE
    max_amount = true_revenue_monthly * MAX_REVENUE_MULTIPLE

    # Capacity cap. Offer's implied monthly payment = ``offer * 0.15``;
    # must fit within ``remaining_capacity``.
    capacity_cap = remaining_capacity / DEFAULT_HOLDBACK_PCT

    rationale_parts: list[str] = []

    capacity_limited = False
    if base_amount > capacity_cap:
        base_amount = capacity_cap
        capacity_limited = True
    if max_amount > capacity_cap:
        max_amount = capacity_cap
        capacity_limited = True
    if capacity_limited:
        rationale_parts.append(
            f"capped at ${capacity_cap.quantize(Decimal('1'))} to fit "
            f"${remaining_capacity.quantize(Decimal('1'))}/mo remaining "
            f"holdback capacity"
        )

    # Existing-stack overload discount. Applies to ``recommended_amount``
    # only — ``max_amount`` stays at the capacity cap so the underwriter
    # can see how much room they'd have if they accept the overload risk.
    combined_holdback = mca_stack.estimated_combined_holdback_pct
    if combined_holdback is not None and combined_holdback > HIGH_COMBINED_HOLDBACK_THRESHOLD:
        base_amount = base_amount * HIGH_HOLDBACK_DISCOUNT_FACTOR
        rationale_parts.append(
            f"discounted 25% — existing combined holdback "
            f"({combined_holdback}%) exceeds "
            f"{HIGH_COMBINED_HOLDBACK_THRESHOLD}% threshold"
        )

    recommended_rounded = _round_to_increment(base_amount, ROUNDING_INCREMENT)
    max_rounded = _round_to_increment(max_amount, ROUNDING_INCREMENT)

    if recommended_rounded < MIN_OFFER_FLOOR:
        return None

    if not rationale_parts:
        rationale_parts.append(
            f"sized at 1.0x monthly revenue (${true_revenue_monthly.quantize(Decimal('1'))})"
        )

    rationale = "; ".join(rationale_parts)[:_RATIONALE_MAX_LENGTH]

    return OfferRecommendation(
        product_type="revenue_based",
        recommended_amount=recommended_rounded,
        max_amount=max_rounded,
        holdback_pct=DEFAULT_HOLDBACK_PCT,
        rationale=rationale,
    )


def _compute_offer_business_loan(*, true_revenue_monthly: Decimal) -> OfferRecommendation | None:
    """Term loan: 3x monthly revenue, 16% APR, 12mo (PLACEHOLDERS)."""
    if true_revenue_monthly <= 0:
        return None
    loan_amount_raw = true_revenue_monthly * BUSINESS_LOAN_REVENUE_MULTIPLE
    loan_amount = _round_to_increment(loan_amount_raw, ROUNDING_INCREMENT)
    rate_monthly = BUSINESS_LOAN_APR_PLACEHOLDER / Decimal(12)
    monthly_payment = _pmt(rate_monthly, BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER, loan_amount)
    total_cost = (monthly_payment * Decimal(BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER)).quantize(
        Decimal("0.01")
    )
    holdback_pct = (monthly_payment / true_revenue_monthly).quantize(Decimal("0.0001"))
    holdback_pct = min(holdback_pct, Decimal("1"))
    rationale = (
        f"business loan PLACEHOLDER: "
        f"${loan_amount.quantize(Decimal('1'))} at "
        f"{(BUSINESS_LOAN_APR_PLACEHOLDER * 100).quantize(Decimal('1'))}% APR / "
        f"{BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER}mo — rate + term pending operator calibration"
    )
    return OfferRecommendation(
        product_type="business_loan",
        recommended_amount=loan_amount,
        max_amount=loan_amount,
        holdback_pct=holdback_pct,
        rationale=rationale[:_RATIONALE_MAX_LENGTH],
        loan_amount=loan_amount,
        interest_rate_apr=BUSINESS_LOAN_APR_PLACEHOLDER,
        term_months=BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER,
        monthly_payment=monthly_payment,
        total_cost=total_cost,
    )


def _compute_offer_line_of_credit(*, true_revenue_monthly: Decimal) -> OfferRecommendation | None:
    """LOC: 1.5x monthly revenue credit limit (PLACEHOLDER)."""
    if true_revenue_monthly <= 0:
        return None
    credit_limit_raw = true_revenue_monthly * LOC_REVENUE_MULTIPLE
    credit_limit = _round_to_increment(credit_limit_raw, ROUNDING_INCREMENT)
    rationale = (
        f"line of credit PLACEHOLDER: "
        f"${credit_limit.quantize(Decimal('1'))} limit at "
        f"{(LOC_APR_PLACEHOLDER * 100).quantize(Decimal('1'))}% APR — "
        f"draw rate operator-set per draw, pending operator calibration"
    )
    return OfferRecommendation(
        product_type="line_of_credit",
        recommended_amount=credit_limit,
        max_amount=credit_limit,
        # LOC has no fixed holdback; per-draw monthly cost depends on
        # utilization. 0 keeps the chip honest until real config lands.
        holdback_pct=Decimal("0.0"),
        rationale=rationale[:_RATIONALE_MAX_LENGTH],
        credit_limit=credit_limit,
        draw_rate=Decimal("0.0"),
        interest_rate_apr=LOC_APR_PLACEHOLDER,
    )


def _compute_offer_equipment(
    *,
    true_revenue_monthly: Decimal,
    product_kwargs: dict[str, Any],
) -> OfferRecommendation:
    """Equipment finance: operator-supplied equipment cost, 10% down,
    12% APR, 60mo (all PLACEHOLDERS).

    Raises ``ValueError`` when ``equipment_cost`` is missing — the
    operator must supply it for this product (no defensible default).
    """
    equipment_cost = product_kwargs.get("equipment_cost")
    if equipment_cost is None:
        raise ValueError("equipment product_type requires equipment_cost in product_kwargs")
    equipment_cost_decimal = Decimal(equipment_cost)
    financed_raw = equipment_cost_decimal * (Decimal(1) - EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER)
    financed_amount = _round_to_increment(financed_raw, ROUNDING_INCREMENT)
    rate_monthly = EQUIPMENT_APR_PLACEHOLDER / Decimal(12)
    monthly_payment = _pmt(rate_monthly, EQUIPMENT_TERM_MONTHS_PLACEHOLDER, financed_amount)
    total_cost = (monthly_payment * Decimal(EQUIPMENT_TERM_MONTHS_PLACEHOLDER)).quantize(
        Decimal("0.01")
    )
    if true_revenue_monthly > 0:
        holdback_pct = (monthly_payment / true_revenue_monthly).quantize(Decimal("0.0001"))
        holdback_pct = min(holdback_pct, Decimal("1"))
    else:
        holdback_pct = Decimal("0.0")
    rationale = (
        f"equipment finance PLACEHOLDER: "
        f"${financed_amount.quantize(Decimal('1'))} financed "
        f"({(EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER * 100).quantize(Decimal('1'))}% down on "
        f"${equipment_cost_decimal.quantize(Decimal('1'))} equipment) at "
        f"{(EQUIPMENT_APR_PLACEHOLDER * 100).quantize(Decimal('1'))}% APR / "
        f"{EQUIPMENT_TERM_MONTHS_PLACEHOLDER}mo — rate + term pending operator calibration"
    )
    return OfferRecommendation(
        product_type="equipment",
        recommended_amount=financed_amount,
        max_amount=financed_amount,
        holdback_pct=holdback_pct,
        rationale=rationale[:_RATIONALE_MAX_LENGTH],
        financed_amount=financed_amount,
        down_payment_pct=EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER,
        interest_rate_apr=EQUIPMENT_APR_PLACEHOLDER,
        term_months=EQUIPMENT_TERM_MONTHS_PLACEHOLDER,
        monthly_payment=monthly_payment,
        total_cost=total_cost,
    )


def _compute_offer_asset_based(
    *,
    true_revenue_monthly: Decimal,
    product_kwargs: dict[str, Any],
) -> OfferRecommendation:
    """Asset-based revolver: 80% advance against operator-supplied
    eligible collateral (PLACEHOLDER advance rate).

    Raises ``ValueError`` when ``eligible_collateral`` is missing.
    """
    eligible_collateral = product_kwargs.get("eligible_collateral")
    if eligible_collateral is None:
        raise ValueError("asset_based product_type requires eligible_collateral in product_kwargs")
    eligible_collateral_decimal = Decimal(eligible_collateral)
    revolving_raw = eligible_collateral_decimal * ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER
    revolving_limit = _round_to_increment(revolving_raw, ROUNDING_INCREMENT)
    rationale = (
        f"asset-based PLACEHOLDER: "
        f"${revolving_limit.quantize(Decimal('1'))} revolving limit at "
        f"{(ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER * 100).quantize(Decimal('1'))}% advance on "
        f"${eligible_collateral_decimal.quantize(Decimal('1'))} eligible collateral — "
        f"advance rate pending operator calibration"
    )
    return OfferRecommendation(
        product_type="asset_based",
        recommended_amount=revolving_limit,
        max_amount=revolving_limit,
        holdback_pct=Decimal("0.0"),
        rationale=rationale[:_RATIONALE_MAX_LENGTH],
        revolving_limit=revolving_limit,
        advance_rate_pct=ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER,
        eligible_collateral_estimate=eligible_collateral_decimal.quantize(Decimal("0.01")),
    )


def _compute_offer_receivables(
    *,
    true_revenue_monthly: Decimal,
    product_kwargs: dict[str, Any],
) -> OfferRecommendation:
    """Invoice factoring: 80% advance / 10% reserve / 3% per 30d fee
    (operator-overridable DEFAULTS).

    When ``invoice_face_value`` is supplied in ``product_kwargs``, sizes
    ``recommended_amount`` as ``face x advance_rate``. Otherwise sizes
    a representative offer at 1x monthly revenue x advance_rate so the
    chip still renders — operator selects the actual invoice batch at
    submission time.
    """
    advance_rate = Decimal(
        product_kwargs.get("advance_rate_pct", RECEIVABLES_ADVANCE_RATE_PCT_DEFAULT)
    )
    reserve_pct = Decimal(product_kwargs.get("reserve_pct", RECEIVABLES_RESERVE_PCT_DEFAULT))
    factoring_fee_pct = Decimal(
        product_kwargs.get("factoring_fee_pct", RECEIVABLES_FACTORING_FEE_PCT_PER_30D_DEFAULT)
    )
    invoice_face = product_kwargs.get("invoice_face_value")
    if invoice_face is not None:
        face_decimal = Decimal(invoice_face)
        advance_raw = face_decimal * advance_rate
        sizing_source = f"${face_decimal.quantize(Decimal('1'))} invoice face value"
    elif true_revenue_monthly > 0:
        # No invoice supplied — size a representative monthly advance off
        # revenue so the operator sees a meaningful number. Real submission
        # uses the actual invoice batch.
        face_decimal = true_revenue_monthly
        advance_raw = face_decimal * advance_rate
        sizing_source = (
            f"${face_decimal.quantize(Decimal('1'))} representative monthly invoice batch"
        )
    else:
        face_decimal = Decimal("0")
        advance_raw = Decimal("0")
        sizing_source = "no invoice / revenue available — operator-set at submission"
    advance_amount = _round_to_increment(advance_raw, ROUNDING_INCREMENT)
    rationale = (
        f"receivables factoring DEFAULTS: "
        f"{(advance_rate * 100).quantize(Decimal('1'))}% advance / "
        f"{(reserve_pct * 100).quantize(Decimal('1'))}% reserve / "
        f"{(factoring_fee_pct * 100).quantize(Decimal('0.1'))}% per 30d on "
        f"{sizing_source}"
    )
    return OfferRecommendation(
        product_type="receivables",
        recommended_amount=advance_amount,
        max_amount=advance_amount,
        holdback_pct=Decimal("0.0"),
        rationale=rationale[:_RATIONALE_MAX_LENGTH],
        advance_rate_pct=advance_rate,
        reserve_pct=reserve_pct,
        factoring_fee_pct=factoring_fee_pct,
    )


def _round_to_increment(amount: Decimal, increment: Decimal) -> Decimal:
    """Round ``amount`` to the nearest multiple of ``increment``.

    Uses ``ROUND_HALF_UP`` so $5,250 → $5,500 (tie goes up). MCA
    underwriting convention.
    """
    quotient = (amount / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (quotient * increment).quantize(Decimal("0.01"))


__all__ = [
    "ASSET_BASED_ADVANCE_RATE_PCT_PLACEHOLDER",
    "BASE_REVENUE_MULTIPLE",
    "BUSINESS_LOAN_APR_PLACEHOLDER",
    "BUSINESS_LOAN_REVENUE_MULTIPLE",
    "BUSINESS_LOAN_TERM_MONTHS_PLACEHOLDER",
    "DEFAULT_HOLDBACK_PCT",
    "EQUIPMENT_APR_PLACEHOLDER",
    "EQUIPMENT_DOWN_PAYMENT_PCT_PLACEHOLDER",
    "EQUIPMENT_TERM_MONTHS_PLACEHOLDER",
    "HIGH_COMBINED_HOLDBACK_THRESHOLD",
    "HIGH_HOLDBACK_DISCOUNT_FACTOR",
    "LOC_APR_PLACEHOLDER",
    "LOC_REVENUE_MULTIPLE",
    "MAX_REVENUE_MULTIPLE",
    "MIN_OFFER_FLOOR",
    "RECEIVABLES_ADVANCE_RATE_PCT_DEFAULT",
    "RECEIVABLES_FACTORING_FEE_PCT_PER_30D_DEFAULT",
    "RECEIVABLES_RESERVE_PCT_DEFAULT",
    "ROUNDING_INCREMENT",
    "OfferRecommendation",
    "compute_offer",
]
