"""Line of Credit (LOC) underwriting scoring (2026-07-01 A1e).

LOC lenders care about:
- Credit score: 650+ minimum, 700+ for best rates
- TIB: 12+ months, 24+ for larger lines
- Revenue consistency: predictable monthly cash flow
- Existing utilization: current LOC draw vs limit (if any)
- Borrowing base: A/R + eligible inventory drives line ceiling
- Clean payment history: no recent lates, no NSF clusters

A LOC is a revolving facility — the line amount is not a one-time
advance. Monthly payment is interest-only on drawn balance.
Typical: 1-5 year term, $10K-$5M line, weekly/monthly payments.

The dossier renders a product-analysis section when
``merchant.product_type == 'line_of_credit'`` — eligibility,
recommended line, rate range, referral lenders, and any soft concerns.

Not persisted; pure derivation from live merchant + operator inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from aegis.merchants.models import MerchantRow

LOC_MIN_FICO: int = 650
LOC_PREFERRED_FICO: int = 700
LOC_MIN_TIB_MONTHS: int = 12
LOC_PREFERRED_TIB_MONTHS: int = 24
# Max line = 1.5x monthly revenue (revenue-based cap).
LOC_MAX_LINE_TO_REVENUE_RATIO: Decimal = Decimal("1.5")
# More than 3 NSFs in the recent window is disqualifying.
LOC_MAX_NSF: int = 3
# 3+ active MCA positions is disqualifying for LOC lenders.
LOC_MAX_MCA_POSITIONS: int = 2


@dataclass
class LOCScoringResult:
    """Product-analysis output for a line-of-credit-shape deal."""

    eligible: bool
    blockers: list[str] = field(default_factory=list)
    soft_concerns: list[str] = field(default_factory=list)
    recommended_line: Decimal | None = None
    estimated_rate_low: Decimal | None = None
    estimated_rate_high: Decimal | None = None
    draw_fee: Decimal | None = None
    typical_term_months: int | None = None
    referral_lenders: list[str] = field(default_factory=list)


def score_loc_deal(
    merchant: MerchantRow,
    true_revenue_monthly: Decimal | None = None,
    nsf_count_3mo: int = 0,
    avg_daily_balance: Decimal | None = None,
    ar_current: Decimal | None = None,
    existing_loc_balance: Decimal | None = None,
) -> LOCScoringResult:
    """Score one merchant against LOC underwriting criteria."""
    blockers: list[str] = []
    concerns: list[str] = []

    fico = merchant.credit_score or 0
    tib = merchant.time_in_business_months or 0
    revenue = true_revenue_monthly or Decimal("0")
    requested = (
        Decimal(str(merchant.requested_amount)) if merchant.requested_amount else Decimal("0")
    )
    positions = merchant.stated_mca_positions or 0

    if fico < LOC_MIN_FICO:
        blockers.append(
            f"FICO {fico} below LOC minimum ({LOC_MIN_FICO}). "
            f"Most LOC lenders require {LOC_MIN_FICO}+."
        )
    if tib < LOC_MIN_TIB_MONTHS:
        blockers.append(f"TIB {tib} months below LOC minimum ({LOC_MIN_TIB_MONTHS} months).")
    if revenue == Decimal("0"):
        blockers.append(
            "Zero measured revenue — cannot establish borrowing base or repayment capacity."
        )
    if nsf_count_3mo > LOC_MAX_NSF:
        blockers.append(
            f"{nsf_count_3mo} NSFs in recent window exceeds LOC threshold "
            f"({LOC_MAX_NSF}). LOC lenders require clean payment history."
        )
    if positions > LOC_MAX_MCA_POSITIONS:
        blockers.append(
            f"{positions} existing MCA positions — LOC lenders typically require "
            f"no more than {LOC_MAX_MCA_POSITIONS} active positions."
        )

    if blockers:
        return LOCScoringResult(eligible=False, blockers=blockers)

    if fico < LOC_PREFERRED_FICO:
        concerns.append(
            f"FICO {fico} is below preferred ({LOC_PREFERRED_FICO}). "
            f"Expect higher rate (prime + 3-5% vs prime + 1-2% for 700+)."
        )
    if tib < LOC_PREFERRED_TIB_MONTHS:
        concerns.append(
            f"TIB {tib} months is below preferred {LOC_PREFERRED_TIB_MONTHS} "
            f"months. Lenders may cap line at lower amount."
        )
    if avg_daily_balance is not None and revenue > Decimal("0"):
        balance_ratio = avg_daily_balance / revenue
        if balance_ratio < Decimal("0.15"):
            concerns.append(
                f"Average daily balance is {balance_ratio:.0%} of monthly "
                f"revenue — low cash cushion. LOC lenders prefer 15%+."
            )
    if existing_loc_balance is not None and existing_loc_balance > Decimal("0"):
        concerns.append(
            f"Existing LOC balance ${existing_loc_balance:,.0f} — lender will "
            f"check utilization rate. High utilization (>80%) may reduce new line."
        )

    # Line sizing.
    # Revenue-based cap: max 1.5x monthly revenue.
    revenue_cap = revenue * LOC_MAX_LINE_TO_REVENUE_RATIO
    # A/R-based cap: 85% of current eligible A/R if available.
    ar_cap = (ar_current * Decimal("0.85")) if ar_current else None
    if ar_cap is not None:
        base_line = max(revenue_cap, ar_cap)
    else:
        base_line = revenue_cap

    recommended = min(requested or base_line, base_line, Decimal("5000000"))

    if fico >= LOC_PREFERRED_FICO:
        rate_low, rate_high = Decimal("0.08"), Decimal("0.15")
        lenders = [
            "Bluevine",
            "OnDeck",
            "Fundbox",
            "American Express Business Blueprint",
            "Wells Fargo Business Line",
        ]
    else:
        rate_low, rate_high = Decimal("0.15"), Decimal("0.30")
        lenders = [
            "Bluevine",
            "Fundbox",
            "Headway Capital",
            "National Funding",
            "Credibly",
        ]

    return LOCScoringResult(
        eligible=True,
        soft_concerns=concerns,
        recommended_line=recommended,
        estimated_rate_low=rate_low,
        estimated_rate_high=rate_high,
        draw_fee=Decimal("0.00"),
        typical_term_months=12,
        referral_lenders=lenders,
    )


__all__ = [
    "LOC_MAX_LINE_TO_REVENUE_RATIO",
    "LOC_MAX_MCA_POSITIONS",
    "LOC_MAX_NSF",
    "LOC_MIN_FICO",
    "LOC_MIN_TIB_MONTHS",
    "LOC_PREFERRED_FICO",
    "LOC_PREFERRED_TIB_MONTHS",
    "LOCScoringResult",
    "score_loc_deal",
]
