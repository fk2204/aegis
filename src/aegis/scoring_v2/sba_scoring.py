"""SBA-specific underwriting scoring (2026-07-01 A1a).

SBA lenders care about: DSCR, TIB (24+ months), credit (650+ Express,
680+ 7(a)), a clean balance sheet (no active MCA positions), NAICS
eligibility. Bank statements are secondary — P&L and tax returns are
the primary underwriting inputs.

Complementary to ``aegis.scoring_v2.sba_eligibility`` which does the
initial pre-screen for the dossier "SBA referral" pill. This module
takes it further: program selection, recommended amount, rate range,
and specific referral lenders per program.

Read via the dossier route when ``merchant.product_type ==
'business_loan'``; the dossier renders a product-analysis section
with eligibility, program, referral lenders, rate range, and any
soft concerns.

Not persisted — pure derivation from live merchant + analysis state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from aegis.merchants.models import MerchantRow

SBAProgram = Literal["7a", "express", "microloans", "none"]

SBA_EXPRESS_MIN_FICO: int = 650
SBA_7A_MIN_FICO: int = 680
SBA_MIN_TIB_MONTHS: int = 24
# Active MCAs disqualify SBA — the SBA lender requires a clean balance
# sheet. 0 is the strict interpretation; a soft concern fires at 1-2
# per operator preference.
SBA_MAX_MCA_POSITIONS: int = 0
SBA_MIN_MONTHLY_REVENUE_7A: Decimal = Decimal("50000")


@dataclass
class SBAScoringResult:
    """Product-analysis output for an SBA-shape deal.

    Frozen-in-spirit — the dossier render path treats this as
    read-only. Construct via ``score_sba_deal``.
    """

    eligible: bool
    program: SBAProgram
    blockers: list[str] = field(default_factory=list)
    soft_concerns: list[str] = field(default_factory=list)
    recommended_amount: Decimal | None = None
    estimated_rate_low: Decimal | None = None
    estimated_rate_high: Decimal | None = None
    referral_lenders: list[str] = field(default_factory=list)


def score_sba_deal(
    merchant: MerchantRow,
    true_revenue_monthly: Decimal | None = None,
    confirmed_mca_count: int = 0,
) -> SBAScoringResult:
    """Score one merchant against SBA underwriting criteria.

    ``true_revenue_monthly`` is the bank-derived measured revenue.
    Falls through to ``merchant.monthly_revenue`` (stated) when
    unavailable — the stated value is a soft floor for the
    "zero revenue" hard-blocker.

    ``confirmed_mca_count`` is the Track B named-funder stack count.
    Falls through to ``merchant.stated_mca_positions``.
    """
    blockers: list[str] = []
    concerns: list[str] = []

    fico = merchant.credit_score or 0
    tib = merchant.time_in_business_months or 0
    revenue = true_revenue_monthly or (
        Decimal(str(merchant.monthly_revenue)) if merchant.monthly_revenue else Decimal("0")
    )
    requested = (
        Decimal(str(merchant.requested_amount)) if merchant.requested_amount else Decimal("0")
    )
    positions = merchant.stated_mca_positions or confirmed_mca_count

    # Hard blockers — any single fire → ineligible.
    if fico < SBA_EXPRESS_MIN_FICO:
        blockers.append(f"FICO {fico} below SBA Express minimum {SBA_EXPRESS_MIN_FICO}")
    if tib < SBA_MIN_TIB_MONTHS:
        blockers.append(f"TIB {tib} months below SBA minimum {SBA_MIN_TIB_MONTHS} months")
    if positions > SBA_MAX_MCA_POSITIONS:
        blockers.append(f"{positions} active MCA position(s) — SBA requires clean balance sheet")
    if revenue == Decimal("0"):
        blockers.append("Zero measured revenue — cannot establish repayment capacity")

    if blockers:
        return SBAScoringResult(eligible=False, program="none", blockers=blockers)

    # Program selection cascade (best fit first).
    if fico >= SBA_7A_MIN_FICO and revenue >= SBA_MIN_MONTHLY_REVENUE_7A:
        program: SBAProgram = "7a"
        max_amount = Decimal("5000000")
        lenders = [
            "Live Oak Bank",
            "Byline Bank",
            "Huntington National Bank",
            "Celtic Bank",
        ]
        rate_low, rate_high = Decimal("0.065"), Decimal("0.085")
    elif fico >= SBA_EXPRESS_MIN_FICO:
        program = "express"
        max_amount = Decimal("500000")
        lenders = [
            "Celtic Bank",
            "Newtek Bank",
            "Readycap Commercial",
            "Fountainhead",
        ]
        rate_low, rate_high = Decimal("0.075"), Decimal("0.095")
    else:  # pragma: no cover — unreachable given the FICO blocker above
        program = "microloans"
        max_amount = Decimal("50000")
        lenders = ["Local SBA Microlender"]
        rate_low, rate_high = Decimal("0.08"), Decimal("0.13")

    # Soft concerns — informational only, don't block.
    if fico < SBA_7A_MIN_FICO:
        concerns.append(f"FICO {fico} qualifies for {program} only, not standard 7(a)")

    # Recommended amount = min of requested / program cap / 36x revenue.
    # 36x is the SBA-convention ceiling for a term-loan structure.
    recommended = min(
        requested or max_amount,
        max_amount,
        revenue * Decimal("36"),
    )

    return SBAScoringResult(
        eligible=True,
        program=program,
        soft_concerns=concerns,
        recommended_amount=recommended,
        estimated_rate_low=rate_low,
        estimated_rate_high=rate_high,
        referral_lenders=lenders,
    )


__all__ = [
    "SBA_7A_MIN_FICO",
    "SBA_EXPRESS_MIN_FICO",
    "SBA_MAX_MCA_POSITIONS",
    "SBA_MIN_MONTHLY_REVENUE_7A",
    "SBA_MIN_TIB_MONTHS",
    "SBAProgram",
    "SBAScoringResult",
    "score_sba_deal",
]
