"""Funder submission email package generator.

REWRITES the term/payback math from the TS version. TS computed
`payback_business_days = principal / daily_payback`, which silently
undercounts payback by the factor margin (the same bug as score.py's
`estimated_payback_days`). Hercules uses
`total_repayment / daily_payment` everywhere. Numbers in the email body
must reconcile against `score_result.estimated_payback_days`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from aegis.money import Money, safe_divide
from aegis.scoring.models import (
    FunderMatch,
    ScoreInput,
    ScoreResult,
    SubmissionPackage,
)

_BUSINESS_DAYS_PER_MONTH = Decimal("22")


@dataclass(frozen=True)
class _Terms:
    principal: Money
    factor: Decimal
    total_repayment: Money
    holdback_pct: Decimal
    daily_revenue: Money
    daily_payment: Money
    estimated_payback_days: int


def build_submission_package(
    deal: ScoreInput,
    score: ScoreResult,
    matched_funder: FunderMatch,
) -> SubmissionPackage:
    """Generate a submission email package with reconciled term/payback numbers."""
    terms = _compute_terms(deal, score)

    subject = _subject(deal, score, matched_funder)
    body = _body(deal, score, matched_funder, terms)

    return SubmissionPackage(
        id=uuid4(),
        score_input=deal,
        score_result=score,
        matched_funders=[matched_funder],
        email_subject=subject,
        email_body=body,
    )


def _compute_terms(deal: ScoreInput, score: ScoreResult) -> _Terms:
    """All term math in one place. Test against this struct."""
    principal = score.suggested_max_advance or deal.requested_amount
    factor = score.recommended_factor_rate or deal.requested_factor
    holdback = score.recommended_holdback_pct
    total_repayment = (principal * factor).quantize(Decimal("0.01"))
    daily_revenue = safe_divide(deal.monthly_revenue, _BUSINESS_DAYS_PER_MONTH)
    daily_payment = (
        (daily_revenue * holdback).quantize(Decimal("0.01"))
        if holdback > 0
        else Decimal("0.00")
    )
    if daily_payment == 0:
        payback = 0
    else:
        # CORRECT: total_repayment / daily_payment. NOT principal / daily_payment.
        days = total_repayment / daily_payment
        payback = int(days.to_integral_value())
    return _Terms(
        principal=principal,
        factor=factor,
        total_repayment=total_repayment,
        holdback_pct=holdback,
        daily_revenue=daily_revenue.quantize(Decimal("0.01")),
        daily_payment=daily_payment,
        estimated_payback_days=payback,
    )


def _subject(deal: ScoreInput, score: ScoreResult, funder: FunderMatch) -> str:
    return (
        f"[{score.tier}-tier] {deal.business_name} — "
        f"${deal.requested_amount} @ {deal.requested_factor} → {funder.funder_name}"
    )


def _body(
    deal: ScoreInput,
    score: ScoreResult,
    funder: FunderMatch,
    terms: _Terms,
) -> str:
    lines = [
        f"Funder: {funder.funder_name}",
        f"Merchant: {deal.business_name} ({deal.state})",
        "",
        "PROPOSED TERMS",
        f"  principal           ${terms.principal}",
        f"  factor rate         {terms.factor}",
        f"  total repayment     ${terms.total_repayment}",
        f"  daily revenue est.  ${terms.daily_revenue}",
        f"  holdback %          {terms.holdback_pct * 100:.1f}%",
        f"  daily payment       ${terms.daily_payment}",
        f"  est. payback days   {terms.estimated_payback_days}",
        "",
        "AEGIS SCORING",
        f"  score               {score.score} ({score.tier})",
        f"  recommendation      {score.recommendation}",
    ]
    if score.soft_concerns:
        lines.append("  soft concerns       " + ", ".join(score.soft_concerns))
    if funder.soft_concerns:
        lines.append("  funder concerns     " + ", ".join(funder.soft_concerns))
    return "\n".join(lines)


__all__ = ["build_submission_package"]
