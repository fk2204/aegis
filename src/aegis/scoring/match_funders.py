"""Funder matching — hard fails + soft concerns separated.

A funder match is a tuple of `(qualifies, hard_fails, soft_concerns)`.
Hard fails mean the funder will not approve regardless of relationship;
soft concerns degrade likelihood but don't reject.

TS-fix
------
Missing credit_score / time_in_business is a SOFT CONCERN, not a silent
pass. Scoring missing data as "no concern" is how stacking gets through.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from aegis.money import Money
from aegis.scoring.models import FunderMatch, ScoreInput, ScoreResult


@dataclass
class FunderRow:
    """Subset of a funders-table row relevant to matching."""

    id: UUID
    name: str
    active: bool = True
    min_monthly_revenue: Money | None = None
    min_avg_daily_balance: Money | None = None
    min_credit_score: int | None = None
    min_months_in_business: int | None = None
    max_positions: int | None = None
    accepts_stacking: bool = False
    min_advance: Money | None = None
    max_advance: Money | None = None
    max_nsf_tolerance: int | None = None
    excluded_industries: tuple[str, ...] = ()
    excluded_states: tuple[str, ...] = ()


def match_funder(
    funder: FunderRow,
    deal: ScoreInput,
    score: ScoreResult,
) -> FunderMatch | None:
    """Match a deal against a single funder. None if funder has no criteria configured."""
    if not funder.active:
        return None

    hard: list[str] = []
    soft: list[str] = []
    criteria_count = 0

    if funder.min_monthly_revenue is not None:
        criteria_count += 1
        if deal.monthly_revenue < funder.min_monthly_revenue:
            hard.append(
                f"revenue ${deal.monthly_revenue} < min ${funder.min_monthly_revenue}"
            )

    if funder.min_avg_daily_balance is not None:
        criteria_count += 1
        if deal.avg_daily_balance < funder.min_avg_daily_balance:
            hard.append(
                f"adb ${deal.avg_daily_balance} < min ${funder.min_avg_daily_balance}"
            )

    if funder.min_credit_score is not None:
        criteria_count += 1
        if deal.credit_score is None:
            soft.append("credit_score_unknown")
        elif deal.credit_score < funder.min_credit_score:
            hard.append(
                f"credit {deal.credit_score} < min {funder.min_credit_score}"
            )

    if funder.min_months_in_business is not None:
        criteria_count += 1
        if deal.time_in_business_months is None:
            soft.append("time_in_business_unknown")
        elif deal.time_in_business_months < funder.min_months_in_business:
            hard.append(
                f"tib {deal.time_in_business_months}mo < min {funder.min_months_in_business}mo"
            )

    if funder.max_positions is not None:
        criteria_count += 1
        if deal.mca_positions > funder.max_positions:
            hard.append(f"positions {deal.mca_positions} > max {funder.max_positions}")
    elif not funder.accepts_stacking and deal.mca_positions > 0:
        criteria_count += 1
        hard.append("funder_does_not_accept_stacking")

    if funder.max_nsf_tolerance is not None:
        criteria_count += 1
        if deal.num_nsf > funder.max_nsf_tolerance:
            hard.append(f"nsf {deal.num_nsf} > max {funder.max_nsf_tolerance}")

    if funder.min_advance is not None:
        criteria_count += 1
        if deal.requested_amount < funder.min_advance:
            hard.append(
                f"requested ${deal.requested_amount} < min advance ${funder.min_advance}"
            )

    if funder.max_advance is not None:
        criteria_count += 1
        if deal.requested_amount > funder.max_advance:
            hard.append(
                f"requested ${deal.requested_amount} > max advance ${funder.max_advance}"
            )

    if funder.excluded_industries:
        criteria_count += 1
        naics = (deal.industry_naics or "").lower()
        if any(ind.lower() == naics for ind in funder.excluded_industries):
            hard.append(f"industry_excluded: {deal.industry_naics}")

    if funder.excluded_states:
        criteria_count += 1
        if deal.state.upper() in {s.upper() for s in funder.excluded_states}:
            hard.append(f"state_excluded: {deal.state}")

    if criteria_count == 0:
        return None

    qualifies = len(hard) == 0
    likelihood = _likelihood(qualifies, soft, score.tier)
    return FunderMatch(
        funder_id=funder.id,
        funder_name=funder.name,
        match_score=likelihood,
        reasons=[f"tier_{score.tier}"] if qualifies else [],
        soft_concerns=hard + soft,  # union — caller wants the full picture
    )


def _likelihood(qualifies: bool, soft: list[str], tier: str) -> int:
    if not qualifies:
        return 0
    base = {"A": 90, "B": 75, "C": 60, "D": 40, "F": 0}[tier]
    return max(0, base - 10 * len(soft))


__all__ = ["FunderRow", "match_funder"]
