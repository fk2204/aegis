"""Per-tier pricing helper — operator-facing factor/holdback/daily hints.

Surfaces "if this deal lands on Logic Advance's Elite tier, here's the
factor range, holdback cap, total payback and daily-payment estimate the
funder typically writes" inside the matched-funders inline panel. This is
pricing GUIDANCE for the operator — NOT a regulator-facing APR
computation. We deliberately do NOT call ``compliance.apr.calculate_apr``
here:

* Factor and holdback are tier-published economics, not Reg Z APR.
* Daily payment is `payback / term_business_days` (MCA convention: 1
  trading year ≈ 252 business days), again not an actuarial computation.
* If the operator wants the regulator-grade APR for a chosen quote, the
  funder-level ``EstimatedTerms.estimated_apr`` already runs Reg Z
  Appendix J via ``calculate_apr`` and surfaces it on the same panel.

This module computes deterministically from tier data; if a tier omits
``buy_rate_low/high`` we return ``None`` for the dependent fields
(payback, daily) rather than fabricating a value. Same posture as
``compute_estimated_terms``: render "—" before guessing.

Decimal-only per CLAUDE.md "NEVER use float for money". The currency
quantizer is 2dp; rate ratios are 4dp (precise enough for "1.2875"
without false precision, mirroring ``match_funders._RATE_QUANT``).

Term convention (R4.2 / U37)
----------------------------
MCA factor pricing assumes the merchant pays back ``advance * factor``
over a business-day window. The standard MCA convention is 252 business
days per year (NYSE trading-day calendar). ``FunderTier`` does NOT
carry a term-days field today; if a future tier adds one, plumb it here
before changing the constant — operator-visible economics must not drift
silently.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from aegis.funders.models import FunderTier
from aegis.money import Money

# Match ``match_funders._RATE_QUANT`` so per-tier factor/holdback rates
# round identically whether they come through ``compute_estimated_terms``
# or ``estimate_tier_pricing``.
_RATE_QUANT: Final[Decimal] = Decimal("0.0001")
_MONEY_QUANT: Final[Decimal] = Decimal("0.01")

# MCA convention. See module docstring "Term convention". If a tier ever
# publishes a term-days field, switch from this constant to the tier value.
DEFAULT_TERM_BUSINESS_DAYS: Final[int] = 252


class TierPricing(BaseModel):
    """Pricing guidance for a single funder tier given an optional advance.

    ``factor_low/high`` mirror the tier's ``buy_rate_low/high`` (quantized
    to 4dp); ``holdback_cap`` mirrors ``max_holdback`` (the tier publishes
    a ceiling, not a range).

    ``payback_total`` and ``daily_payment_estimate`` are computed only when
    BOTH a buy_rate is available AND the caller supplied an ``advance``
    (the merchant's requested or score-suggested amount). When either is
    missing we emit ``None`` rather than substitute a placeholder — render
    "—" in the UI, never a fabricated number.

    Pure data carrier; no I/O; no float. Pydantic v2 strict.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )

    factor_low: Decimal | None = Field(default=None)
    """Lower bound of the tier's buy_rate envelope (e.g. 1.18). ``None``
    when the tier does not publish a low bound."""

    factor_high: Decimal | None = Field(default=None)
    """Upper bound of the tier's buy_rate envelope. ``None`` when the
    tier does not publish a high bound."""

    holdback_cap: Decimal | None = Field(default=None)
    """Tier's ``max_holdback`` as a fraction (0.15 → 15%). The tier
    publishes a ceiling, not a range."""

    payback_total: Money | None = Field(default=None)
    """``advance * buy_rate_midpoint`` — the simple total the merchant
    repays. ``None`` when ``advance`` is missing or the tier omits
    buy_rate."""

    daily_payment_estimate: Money | None = Field(default=None)
    """``payback_total / DEFAULT_TERM_BUSINESS_DAYS`` — the operator's
    "what will the daily debit look like" hint. ``None`` for the same
    reasons as ``payback_total``."""


def estimate_tier_pricing(
    tier: FunderTier,
    advance: Decimal | None,
) -> TierPricing:
    """Deterministically compute per-tier pricing guidance.

    Pure function — no side effects, no I/O. Decimal-only.

    Semantics:

    * ``factor_low``/``factor_high``/``holdback_cap`` always reflect the
      tier's published values (quantized to 4dp). When a tier omits an
      axis, the corresponding field is ``None``.
    * ``payback_total`` requires BOTH an ``advance`` and at least one
      buy_rate bound. We use the midpoint of the buy_rate range when both
      bounds exist; we fall back to the single available bound when only
      one is published. If neither is published, we return ``None``.
    * ``daily_payment_estimate`` divides ``payback_total`` by
      ``DEFAULT_TERM_BUSINESS_DAYS``. Same ``None`` propagation rule.

    Why midpoint rather than tier-position interpolation: the funder-level
    ``compute_estimated_terms`` interpolates by score tier (A → low end,
    F → high end) because the operator wants "what factor will this
    funder quote a B-tier deal". The per-tier matrix already filters to
    tiers the merchant qualifies for; inside a single tier, the merchant
    is on either end of the range based on factors AEGIS doesn't model
    (relationship, doc cleanliness, broker pull). Midpoint is the honest
    "central estimate" hint with the full range still rendered.
    """
    factor_low = _quantize_rate(tier.buy_rate_low)
    factor_high = _quantize_rate(tier.buy_rate_high)
    holdback_cap = _quantize_rate(tier.max_holdback)

    buy_rate_mid = _midpoint(tier.buy_rate_low, tier.buy_rate_high)

    payback_total: Decimal | None = None
    daily_payment: Decimal | None = None
    if advance is not None and advance > 0 and buy_rate_mid is not None:
        payback_total = (advance * buy_rate_mid).quantize(
            _MONEY_QUANT, rounding=ROUND_HALF_UP
        )
        daily_payment = (
            payback_total / Decimal(DEFAULT_TERM_BUSINESS_DAYS)
        ).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)

    return TierPricing(
        factor_low=factor_low,
        factor_high=factor_high,
        holdback_cap=holdback_cap,
        payback_total=payback_total,
        daily_payment_estimate=daily_payment,
    )


def _quantize_rate(value: Decimal | None) -> Decimal | None:
    """Quantize a rate to 4dp; pass ``None`` through unchanged."""
    if value is None:
        return None
    return value.quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)


def _midpoint(low: Decimal | None, high: Decimal | None) -> Decimal | None:
    """Return the midpoint of ``[low, high]``.

    Falls back to the single available bound when only one is published.
    Returns ``None`` when neither bound exists.
    """
    if low is not None and high is not None:
        return (low + high) / Decimal("2")
    if low is not None:
        return low
    if high is not None:
        return high
    return None


__all__ = [
    "DEFAULT_TERM_BUSINESS_DAYS",
    "TierPricing",
    "estimate_tier_pricing",
]
