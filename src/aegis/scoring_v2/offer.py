"""Offer sizing — first-class output for the dossier "suggested offer" chip.

Derives a recommended advance amount and ceiling for a merchant from
three deterministic inputs: monthly true revenue, monthly holdback
capacity (the merchant's operator-confirmed sustainable debt service
load), and the existing MCA-stack rollup. Pure function over those
inputs — no LLM, no DB, no audit-log side effects.

* ``recommended_amount`` — sized at ``true_revenue_monthly * 1.0``,
  then capped by remaining holdback capacity, then knocked down 25%
  when the merchant's existing combined holdback already exceeds 35%.
* ``max_amount`` — sized at ``true_revenue_monthly * 1.5``, then
  capped by the same remaining-capacity ceiling. The 25% overload
  discount intentionally does NOT apply — ``max_amount`` represents
  the aggressive cap an underwriter could stretch to if they accept
  the overload risk; ``recommended_amount`` is the cautious figure.
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
entirely rather than rendering a meaningless tiny number.

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
change, not a code deploy.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money
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
``None`` — too small to size meaningfully."""

ROUNDING_INCREMENT: Final[Decimal] = Decimal("500")
"""Both amounts round to the nearest multiple of this value
(half-up). MCA underwriting convention."""

_RATIONALE_MAX_LENGTH: Final[int] = 320


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
    """

    recommended_amount: Money = Field(
        description=(
            "Cautious advance recommendation. Starts at 1.0x monthly "
            "revenue, capped by remaining holdback capacity, "
            "discounted 25% on existing-stack overload. Rounded to "
            "the nearest $500."
        ),
    )
    max_amount: Money = Field(
        description=(
            "Aggressive ceiling. Starts at 1.5x monthly revenue, capped "
            "by remaining holdback capacity. The 25% overload discount "
            "does NOT apply — ``max_amount`` represents the ceiling an "
            "underwriter could stretch to. Rounded to the nearest $500."
        ),
    )
    holdback_pct: Decimal = Field(
        ge=Decimal("0"),
        le=Decimal("1"),
        description=(
            "Monthly-payment-to-advance ratio assumed when sizing the "
            "offer. ``0.15`` by default. Surfaced so the dossier chip "
            "and the Close ``Recommended Holdback Pct`` custom field "
            "read from one source."
        ),
    )
    rationale: str = Field(
        max_length=_RATIONALE_MAX_LENGTH,
        description=(
            "One-line human-readable explanation naming the limiting "
            "factor — capacity cap, overload discount, or plain "
            "revenue multiple. Read directly into the chip tooltip."
        ),
    )


def compute_offer(
    true_revenue_monthly: Decimal,
    holdback_capacity_monthly: Decimal,
    mca_stack: MCAStackAggregation,
) -> OfferRecommendation | None:
    """Size a recommended + max offer for the merchant.

    Parameters
    ----------
    true_revenue_monthly
        ``parser.aggregate._true_revenue`` projected to a 30-day month
        (i.e. ``AnalysisRow.monthly_revenue``). Zero / negative produces
        ``None`` — no revenue, no offer.
    holdback_capacity_monthly
        Operator-confirmed monthly debt-service budget. Anchors the
        capacity cap together with ``mca_stack.mca_monthly_load``:
        ``remaining = holdback_capacity_monthly - mca_monthly_load``.
        Zero / negative remaining produces ``None`` — no capacity, no
        offer.
    mca_stack
        Existing MCA stack rollup from ``aggregate_mca_stack``. Read
        for ``mca_monthly_load`` (capacity check) and
        ``estimated_combined_holdback_pct`` (overload discount trigger).

    Returns
    -------
    OfferRecommendation | None
        ``None`` for any of:

        * ``true_revenue_monthly <= 0``
        * remaining holdback capacity ``<= 0``
        * ``recommended_amount`` < ``$5,000`` after all sizing steps
          and rounding (deal too small to size).

        Otherwise a populated ``OfferRecommendation`` with both
        amounts rounded to the nearest $500.
    """
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
        recommended_amount=recommended_rounded,
        max_amount=max_rounded,
        holdback_pct=DEFAULT_HOLDBACK_PCT,
        rationale=rationale,
    )


def _round_to_increment(amount: Decimal, increment: Decimal) -> Decimal:
    """Round ``amount`` to the nearest multiple of ``increment``.

    Uses ``ROUND_HALF_UP`` so $5,250 → $5,500 (tie goes up). MCA
    underwriting convention.
    """
    quotient = (amount / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (quotient * increment).quantize(Decimal("0.01"))


__all__ = [
    "BASE_REVENUE_MULTIPLE",
    "DEFAULT_HOLDBACK_PCT",
    "HIGH_COMBINED_HOLDBACK_THRESHOLD",
    "HIGH_HOLDBACK_DISCOUNT_FACTOR",
    "MAX_REVENUE_MULTIPLE",
    "MIN_OFFER_FLOOR",
    "ROUNDING_INCREMENT",
    "OfferRecommendation",
    "compute_offer",
]
