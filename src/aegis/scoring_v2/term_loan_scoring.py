"""Term loan underwriting scoring (2026-07-01 A1f).

Term loan lenders care about:
- DSCR: Net Operating Income / Total Debt Service >= 1.25
  (if NOI is unknown, we proxy from bank statement revenue)
- Credit: 660+ minimum, 700+ preferred
- TIB: 2+ years (some 1 year for shorter terms)
- Collateral: personal guarantee minimum, business assets preferred
- Clean payment history: no recent defaults or bankruptcies
- Existing debt load: total monthly obligations vs revenue

Term loans: $10K-$500K typical via alt lenders,
$500K-$5M via banks/SBA. Fixed rate, fixed payments,
12-60 month terms typically for alt, up to 10 years for bank.

The dossier renders a product-analysis section when
``merchant.product_type == 'term_loan'`` (or a sub-$500K
``business_loan`` — routed to term loan since sub-$500K non-SBA loans
are typically alt-lender term loans).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from aegis.merchants.models import MerchantRow

TermLoanTier = Literal["bank", "alt_lender", "none"]

TERM_MIN_FICO: int = 660
TERM_PREFERRED_FICO: int = 700
TERM_MIN_TIB_MONTHS: int = 24
TERM_MIN_DSCR: Decimal = Decimal("1.25")
TERM_PREFERRED_DSCR: Decimal = Decimal("1.50")
TERM_MAX_NSF: int = 2

# NOI proxy = 25% of monthly revenue (conservative — 75% expense ratio
# is a common alt-lender heuristic when the operator has no P&L).
_NOI_MARGIN: Decimal = Decimal("0.25")
_DEFAULT_TERM_MONTHS: int = 36
_DEFAULT_RATE: Decimal = Decimal("0.10")


@dataclass
class TermLoanScoringResult:
    """Product-analysis output for a term-loan-shape deal."""

    eligible: bool
    tier: TermLoanTier
    blockers: list[str] = field(default_factory=list)
    soft_concerns: list[str] = field(default_factory=list)
    recommended_amount: Decimal | None = None
    estimated_dscr: Decimal | None = None
    estimated_rate_low: Decimal | None = None
    estimated_rate_high: Decimal | None = None
    typical_term_months_range: tuple[int, int] | None = None
    estimated_monthly_payment: Decimal | None = None
    referral_lenders: list[str] = field(default_factory=list)


def _estimate_dscr(
    monthly_revenue: Decimal,
    requested_amount: Decimal,
    term_months: int = _DEFAULT_TERM_MONTHS,
    rate: Decimal = _DEFAULT_RATE,
) -> Decimal:
    """Proxy DSCR from bank statement revenue.

    NOI proxy = monthly revenue * 0.25 (75% expense ratio assumption).
    Monthly debt service = amortized payment on ``requested_amount``.
    Returns Decimal("0") when either revenue or requested is zero, or
    Decimal("999") when the computed payment rounds to zero.
    """
    if monthly_revenue == Decimal("0") or requested_amount == Decimal("0"):
        return Decimal("0")

    monthly_rate = rate / Decimal("12")
    if monthly_rate > Decimal("0"):
        growth = (Decimal("1") + monthly_rate) ** term_months
        payment = requested_amount * (monthly_rate * growth) / (growth - Decimal("1"))
    else:
        payment = requested_amount / Decimal(term_months)

    if payment == Decimal("0"):
        return Decimal("999")

    noi_monthly = monthly_revenue * _NOI_MARGIN
    return (noi_monthly / payment).quantize(Decimal("0.01"))


def score_term_loan_deal(
    merchant: MerchantRow,
    true_revenue_monthly: Decimal | None = None,
    nsf_count_3mo: int = 0,
    confirmed_mca_count: int = 0,
    existing_monthly_debt_service: Decimal | None = None,
) -> TermLoanScoringResult:
    """Score one merchant against term-loan underwriting criteria."""
    blockers: list[str] = []
    concerns: list[str] = []

    fico = merchant.credit_score or 0
    tib = merchant.time_in_business_months or 0
    revenue = true_revenue_monthly or Decimal("0")
    requested = (
        Decimal(str(merchant.requested_amount)) if merchant.requested_amount else Decimal("0")
    )
    positions = max(merchant.stated_mca_positions or 0, confirmed_mca_count)

    if fico < TERM_MIN_FICO:
        blockers.append(f"FICO {fico} below term loan minimum ({TERM_MIN_FICO}).")
    if tib < TERM_MIN_TIB_MONTHS:
        blockers.append(f"TIB {tib} months below term loan minimum ({TERM_MIN_TIB_MONTHS} months).")
    if revenue == Decimal("0"):
        blockers.append("Zero measured revenue — cannot calculate DSCR.")
    if nsf_count_3mo > TERM_MAX_NSF:
        blockers.append(
            f"{nsf_count_3mo} NSFs in recent window. Term loan lenders require "
            f"clean payment history (max {TERM_MAX_NSF})."
        )

    # DSCR check runs even if other blockers fired — the operator sees the
    # actual DSCR number in the result regardless.
    estimated_dscr: Decimal | None = None
    if revenue > Decimal("0") and requested > Decimal("0"):
        estimated_dscr = _estimate_dscr(
            monthly_revenue=revenue,
            requested_amount=requested,
        )
        if estimated_dscr < TERM_MIN_DSCR:
            blockers.append(
                f"Estimated DSCR {estimated_dscr:.2f} below minimum {TERM_MIN_DSCR}. "
                f"Monthly revenue ${revenue:,.0f} is insufficient to service a "
                f"${requested:,.0f} term loan. Reduce request amount or increase "
                f"revenue evidence."
            )

    if blockers:
        return TermLoanScoringResult(
            eligible=False,
            tier="none",
            blockers=blockers,
            estimated_dscr=estimated_dscr,
        )

    # Tier selection.
    if fico >= TERM_PREFERRED_FICO and tib >= 36 and revenue >= Decimal("50000"):
        tier: TermLoanTier = "bank"
        rate_low, rate_high = Decimal("0.07"), Decimal("0.12")
        term_range = (36, 120)
        lenders = [
            "Local community bank",
            "Regional bank SBA preferred lender",
            "Live Oak Bank",
            "Byline Bank",
        ]
    else:
        tier = "alt_lender"
        rate_low, rate_high = Decimal("0.12"), Decimal("0.28")
        term_range = (12, 60)
        lenders = [
            "OnDeck",
            "National Funding",
            "Credibly",
            "Rapid Finance",
            "Fora Financial",
        ]

    if fico < TERM_PREFERRED_FICO:
        concerns.append(
            f"FICO {fico} below preferred ({TERM_PREFERRED_FICO}). Expect "
            f"alt-lender rates (12-28%) vs bank rates (7-12%)."
        )
    if positions >= 2:
        concerns.append(
            f"{positions} existing MCA position(s). Term loan lenders may require "
            f"payoff of MCA positions at closing — factor into deal structure."
        )
    if estimated_dscr is not None and estimated_dscr < TERM_PREFERRED_DSCR:
        concerns.append(
            f"Estimated DSCR {estimated_dscr:.2f} is below preferred "
            f"{TERM_PREFERRED_DSCR}. Deal will likely close but may face tighter "
            f"terms or a lower approval amount."
        )
    if (
        existing_monthly_debt_service is not None
        and revenue > Decimal("0")
        and existing_monthly_debt_service > revenue * Decimal("0.30")
    ):
        ratio = existing_monthly_debt_service / revenue
        concerns.append(
            f"Existing monthly debt service ${existing_monthly_debt_service:,.0f} "
            f"is {ratio:.0%} of monthly revenue — high leverage. New loan payment "
            f"will add to this burden."
        )

    # Recommended amount — bounded by DSCR constraint.
    # Max payment where DSCR stays at TERM_MIN_DSCR given current revenue.
    noi_monthly = revenue * _NOI_MARGIN
    max_payment = noi_monthly / TERM_MIN_DSCR
    mid_rate = (rate_low + rate_high) / Decimal("2")
    monthly_mid_rate = mid_rate / Decimal("12")
    if monthly_mid_rate > Decimal("0"):
        growth = (Decimal("1") + monthly_mid_rate) ** _DEFAULT_TERM_MONTHS
        max_loan = max_payment * (growth - Decimal("1")) / (monthly_mid_rate * growth)
    else:
        max_loan = max_payment * Decimal(_DEFAULT_TERM_MONTHS)

    recommended = min(requested or max_loan, max_loan, Decimal("500000"))

    monthly_payment: Decimal | None = None
    if recommended > Decimal("0") and monthly_mid_rate > Decimal("0"):
        growth = (Decimal("1") + monthly_mid_rate) ** _DEFAULT_TERM_MONTHS
        monthly_payment = (
            recommended * (monthly_mid_rate * growth) / (growth - Decimal("1"))
        ).quantize(Decimal("0.01"))

    return TermLoanScoringResult(
        eligible=True,
        tier=tier,
        soft_concerns=concerns,
        recommended_amount=recommended,
        estimated_dscr=estimated_dscr,
        estimated_rate_low=rate_low,
        estimated_rate_high=rate_high,
        typical_term_months_range=term_range,
        estimated_monthly_payment=monthly_payment,
        referral_lenders=lenders,
    )


__all__ = [
    "TERM_MAX_NSF",
    "TERM_MIN_DSCR",
    "TERM_MIN_FICO",
    "TERM_MIN_TIB_MONTHS",
    "TERM_PREFERRED_DSCR",
    "TERM_PREFERRED_FICO",
    "TermLoanScoringResult",
    "TermLoanTier",
    "_estimate_dscr",
    "score_term_loan_deal",
]
