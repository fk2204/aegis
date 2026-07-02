"""Invoice factoring / receivables financing scoring (2026-07-01 A1c).

Factoring companies care about: A/R aging distribution, customer
concentration, invoice size, eligible industries. Bank statements are
secondary — the A/R aging report is primary.

The dossier renders a product-analysis section when
``merchant.product_type == 'receivables'`` — eligibility, eligible
receivables total, advance rate, estimated advance, and any soft
concerns (concentration, stale receivables).

Not persisted; pure derivation from live merchant + operator A/R input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from aegis.merchants.models import MerchantRow

FACTORING_MIN_ELIGIBLE_AR: Decimal = Decimal("25000")
FACTORING_ADVANCE_RATE: Decimal = Decimal("0.85")
FACTORING_FEE_LOW: Decimal = Decimal("0.015")
FACTORING_FEE_HIGH: Decimal = Decimal("0.035")
# Single-customer share above which we surface a concentration warning
# (the factoring company may apply a haircut).
FACTORING_CONCENTRATION_SOFT_LIMIT: Decimal = Decimal("0.50")
# 90+ day A/R share above which we surface a stale-receivables warning
# (typically ineligible + excluded from advance).
FACTORING_STALE_AR_LIMIT: Decimal = Decimal("0.20")


@dataclass
class FactoringScoringResult:
    """Product-analysis output for a factoring-shape deal."""

    eligible: bool
    blockers: list[str] = field(default_factory=list)
    soft_concerns: list[str] = field(default_factory=list)
    eligible_receivables: Decimal | None = None
    advance_rate: Decimal | None = None
    estimated_advance: Decimal | None = None
    factoring_fee_low: Decimal | None = None
    factoring_fee_high: Decimal | None = None


def score_factoring_deal(
    merchant: MerchantRow,
    ar_current: Decimal | None = None,
    ar_30_60: Decimal | None = None,
    ar_60_90: Decimal | None = None,
    ar_90_plus: Decimal | None = None,
    largest_customer_pct: Decimal | None = None,
) -> FactoringScoringResult:
    """Score one merchant against factoring underwriting criteria.

    ``ar_current`` / ``ar_30_60`` / ``ar_60_90`` / ``ar_90_plus`` are
    the A/R aging bucket totals. Only current + 30-60 count as
    eligible for the advance calculation.

    ``largest_customer_pct`` is the single-customer concentration
    share (0.0-1.0) from the A/R aging report.
    """
    del merchant  # merchant reserved for future use (industry gating, etc.)
    blockers: list[str] = []
    concerns: list[str] = []

    eligible_total = (ar_current or Decimal("0")) + (ar_30_60 or Decimal("0"))

    if eligible_total < FACTORING_MIN_ELIGIBLE_AR:
        blockers.append(
            f"Eligible A/R ${eligible_total:,.0f} below minimum ${FACTORING_MIN_ELIGIBLE_AR:,.0f}"
        )
    if not ar_current and not ar_30_60:
        blockers.append("No A/R aging report uploaded — cannot assess receivables quality")

    if (
        largest_customer_pct is not None
        and largest_customer_pct > FACTORING_CONCENTRATION_SOFT_LIMIT
    ):
        concerns.append(
            f"Single customer {largest_customer_pct:.0%} of receivables — "
            f"concentration risk. Factoring company may apply haircut."
        )

    stale = ar_90_plus or Decimal("0")
    if eligible_total > Decimal("0") and stale > eligible_total * FACTORING_STALE_AR_LIMIT:
        concerns.append(
            f"${stale:,.0f} in 90+ day receivables "
            f"({stale / eligible_total:.0%}) — these are typically ineligible "
            f"and excluded from advance"
        )

    if blockers:
        return FactoringScoringResult(eligible=False, blockers=blockers)

    estimated = eligible_total * FACTORING_ADVANCE_RATE

    return FactoringScoringResult(
        eligible=True,
        soft_concerns=concerns,
        eligible_receivables=eligible_total,
        advance_rate=FACTORING_ADVANCE_RATE,
        estimated_advance=estimated,
        factoring_fee_low=FACTORING_FEE_LOW,
        factoring_fee_high=FACTORING_FEE_HIGH,
    )


__all__ = [
    "FACTORING_ADVANCE_RATE",
    "FACTORING_CONCENTRATION_SOFT_LIMIT",
    "FACTORING_FEE_HIGH",
    "FACTORING_FEE_LOW",
    "FACTORING_MIN_ELIGIBLE_AR",
    "FACTORING_STALE_AR_LIMIT",
    "FactoringScoringResult",
    "score_factoring_deal",
]
