"""Hard-decline + soft-scoring + tier/factor/payback computation.

Two-stage gate:
  1. Hard declines — any one fires returns score=0, tier="F",
     recommendation="decline", and all remaining soft scoring is skipped.
  2. Soft scoring — produces an integer score 0..100, mapped to tier
     A/B/C/D/F. Tier picks a factor rate, holdback %, and computes
     `estimated_payback_days = total_repayment / daily_payment`. THIS IS
     THE TS BUG FIX: TS used `principal / daily_payment`, which undercounts
     payback by the factor margin (~18% for a 1.18 factor).

OFAC SDN screening
------------------
The first hard-decline rule, by design. If the configured `OFACClient`
raises `OFACStaleError` (cache too stale + refresh failed), the call
propagates — scoring never silently allows a sanctioned name through
because the list couldn't refresh. Callers must be ready to catch.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from aegis.money import safe_divide
from aegis.scoring.models import ScoreInput, ScoreResult
from aegis.scoring.ofac import OFACClient

# DSCR thresholds. dscr = monthly_revenue / total_monthly_obligations,
# where total_monthly_obligations includes proposed_daily_payment * 22.
DSCR_HARD_DECLINE: Final[Decimal] = Decimal("1.00")
DSCR_STRONG: Final[Decimal] = Decimal("1.50")
DSCR_ADEQUATE: Final[Decimal] = Decimal("1.15")

# Hard-decline thresholds. Read from here and config — never from elsewhere.
MAX_DEBT_TO_REVENUE: Final[Decimal] = Decimal("0.40")
MIN_MONTHLY_REVENUE: Final[Decimal] = Decimal("10000.00")
FRAUD_SCORE_HARD_DECLINE: Final[int] = 70
DAYS_NEGATIVE_HARD_DECLINE: Final[int] = 15
NSF_COUNT_HARD_DECLINE: Final[int] = 10
RETURNED_ACH_HARD_DECLINE: Final[int] = 5
TIB_MIN_MONTHS: Final[int] = 3
MCA_POSITIONS_HARD_DECLINE: Final[int] = 2  # > 2 active = decline

# Tier-based factor / holdback. AEGIS uses Decimal throughout.
_FACTOR_BY_TIER: Final[dict[str, Decimal]] = {
    "A": Decimal("1.18"),
    "B": Decimal("1.29"),
    "C": Decimal("1.35"),
    "D": Decimal("1.45"),
    "F": Decimal("0.00"),
}
_HOLDBACK_BY_TIER: Final[dict[str, Decimal]] = {
    "A": Decimal("0.10"),
    "B": Decimal("0.12"),
    "C": Decimal("0.15"),
    "D": Decimal("0.20"),
    "F": Decimal("0.00"),
}
_BUSINESS_DAYS_PER_MONTH: Final[Decimal] = Decimal("22")


@dataclass
class _Builder:
    score: int = 50
    flags: list[str] = None  # type: ignore[assignment]
    breakdown: list[dict[str, object]] = None  # type: ignore[assignment]
    soft_concerns: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.flags = []
        self.breakdown = []
        self.soft_concerns = []

    def add(self, delta: int, factor: str) -> None:
        self.score += delta
        self.breakdown.append({"factor": factor, "delta": delta})


def score_deal(
    deal: ScoreInput,
    *,
    ofac: OFACClient | None = None,
) -> ScoreResult:
    """Score a deal. Hard declines first; soft scoring + tier/payback after."""
    hard_declines = _check_hard_declines(deal, ofac)
    if hard_declines:
        return ScoreResult(
            score=0,
            tier="F",
            recommendation="decline",
            hard_decline_reasons=hard_declines,
            soft_concerns=[],
            breakdown=[{"factor": "hard_decline", "delta": -50}],
            suggested_max_advance=Decimal("0.00"),
            recommended_factor_rate=Decimal("0.00"),
            recommended_holdback_pct=Decimal("0.00"),
            estimated_payback_days=0,
        )

    return _soft_score(deal)


# -- hard declines -----------------------------------------------------------


def _check_hard_declines(
    deal: ScoreInput,
    ofac: OFACClient | None,
) -> list[str]:
    reasons: list[str] = []

    if ofac is not None:
        # OFAC screening — checked FIRST so a sanctioned merchant cannot
        # accidentally get other reasons reported and ignored.
        if ofac.is_match(deal.business_name) or ofac.is_match(deal.owner_name):
            reasons.append("ofac_sanctions_match")

    if deal.mca_positions > MCA_POSITIONS_HARD_DECLINE:
        reasons.append(f"stacking_exceeds_limit: {deal.mca_positions} active positions")

    if deal.debt_to_revenue > MAX_DEBT_TO_REVENUE:
        reasons.append(
            f"debt_to_revenue_exceeds_40pct: {(deal.debt_to_revenue * 100):.0f}%"
        )

    if deal.fraud_score >= FRAUD_SCORE_HARD_DECLINE:
        reasons.append(f"fraud_score_critical: {deal.fraud_score}")

    if deal.eof_markers > 1:
        reasons.append(f"incremental_pdf_saves: {deal.eof_markers} EOF markers")

    if deal.monthly_revenue < MIN_MONTHLY_REVENUE:
        reasons.append(f"revenue_below_minimum: ${deal.monthly_revenue}")

    if deal.industry_risk_tier == "avoid":
        reasons.append("industry_excluded")

    if deal.days_negative > DAYS_NEGATIVE_HARD_DECLINE:
        reasons.append(f"days_negative_gt_{DAYS_NEGATIVE_HARD_DECLINE}")

    if deal.num_nsf >= NSF_COUNT_HARD_DECLINE:
        reasons.append(f"nsf_count_gte_{NSF_COUNT_HARD_DECLINE}")

    if deal.returned_ach_count > RETURNED_ACH_HARD_DECLINE:
        reasons.append(f"returned_ach_gt_{RETURNED_ACH_HARD_DECLINE}")

    if deal.time_in_business_months is not None and deal.time_in_business_months < TIB_MIN_MONTHS:
        reasons.append(f"tib_under_{TIB_MIN_MONTHS}_months")

    if not deal.validation_passed:
        reasons.append("validation_failed_manual_review_required")

    if deal.is_renewal and deal.prior_payoff_performance == "default":
        reasons.append("prior_default")

    dscr = _dscr(deal)
    if dscr is not None and dscr < DSCR_HARD_DECLINE:
        reasons.append(f"dscr_below_1: {dscr:.2f}")

    return reasons


# -- soft scoring ------------------------------------------------------------


def _soft_score(deal: ScoreInput) -> ScoreResult:
    b = _Builder()

    _score_revenue(deal, b)
    _score_revenue_trend(deal, b)
    _score_revenue_volatility(deal, b)
    _score_balance(deal, b)
    _score_nsf(deal, b)
    _score_payroll(deal, b)
    _score_stacking(deal, b)
    _score_dscr(deal, b)
    _score_industry(deal, b)
    _score_concentration(deal, b)
    _score_tib(deal, b)
    _score_credit(deal, b)
    _score_renewal(deal, b)
    _score_fraud_soft(deal, b)
    _score_data_quality(deal, b)

    final_score = max(0, min(100, b.score))
    tier = _tier_for(final_score)

    factor = _FACTOR_BY_TIER[tier]
    holdback = _HOLDBACK_BY_TIER[tier]

    suggested_max = _suggested_max_advance(deal.monthly_revenue, tier)
    payback_days = _estimated_payback_days(deal, tier, suggested_max)

    soft_concerns = list(b.soft_concerns)
    # F-tier without any hard decline = "soft-declined": not a sanctions /
    # fraud / stacking issue, just a low aggregate score. Surface this as
    # a distinguishable soft_concern so the email body / merchant detail
    # page can show the right reason ("low aegis score" vs "stacking").
    if tier == "F":
        soft_concerns.append(f"soft_score_below_threshold: score={final_score}")

    return ScoreResult(
        score=final_score,
        tier=tier,
        recommendation=_recommendation_for(tier),
        hard_decline_reasons=[],
        soft_concerns=soft_concerns,
        breakdown=b.breakdown,
        suggested_max_advance=suggested_max,
        recommended_factor_rate=factor,
        recommended_holdback_pct=holdback,
        estimated_payback_days=payback_days,
    )


# -- soft scoring components -------------------------------------------------


def _score_revenue(deal: ScoreInput, b: _Builder) -> None:
    rev = deal.monthly_revenue
    if rev >= 100_000:
        b.add(25, "revenue_100k+")
    elif rev >= 50_000:
        b.add(20, "revenue_50k+")
    elif rev >= 25_000:
        b.add(12, "revenue_25_50k")
    elif rev >= 15_000:
        b.add(5, "revenue_15_25k")
    elif rev >= 10_000:
        b.add(-5, "revenue_10_15k")


def _score_balance(deal: ScoreInput, b: _Builder) -> None:
    bal = deal.avg_daily_balance
    if bal >= 15_000:
        b.add(12, "balance_strong")
    elif bal >= 7_500:
        b.add(8, "balance_good")
    elif bal >= 3_000:
        b.add(3, "balance_ok")
    elif bal >= 1_000:
        b.add(-5, "balance_weak")
    else:
        b.add(-12, "balance_very_weak")
    if deal.lowest_balance < 0:
        b.add(-5, "went_negative")
    if deal.days_negative > 5:
        b.add(-10, "chronic_negative")


def _score_nsf(deal: ScoreInput, b: _Builder) -> None:
    statement_days = deal.statement_days or 30
    nsf_rate = (deal.num_nsf / statement_days) * 30 if statement_days else 0
    if nsf_rate >= 10:
        b.add(-22, "very_high_nsf_rate")
    elif nsf_rate >= 5:
        b.add(-14, "high_nsf_rate")
    elif nsf_rate >= 2:
        b.add(-7, "moderate_nsf_rate")
    elif deal.num_nsf == 0:
        b.add(8, "zero_nsf")


def _score_payroll(deal: ScoreInput, b: _Builder) -> None:
    if deal.payroll_detected:
        b.add(8, "payroll_detected")
    elif deal.monthly_revenue > 50_000:
        b.add(-5, "no_payroll_high_revenue")


def _score_stacking(deal: ScoreInput, b: _Builder) -> None:
    if deal.mca_positions == 0:
        b.add(10, "clean_position")
    elif deal.mca_positions == 1:
        b.add(-8, "one_position")
    elif deal.mca_positions == 2:
        b.add(-18, "double_stacked")


def _score_industry(deal: ScoreInput, b: _Builder) -> None:
    if deal.industry_risk_tier is None:
        return
    delta = {"low": 10, "moderate": 5, "elevated": -3, "high": -10, "avoid": 0}[
        deal.industry_risk_tier
    ]
    b.add(delta, f"industry_{deal.industry_risk_tier}")


def _score_concentration(deal: ScoreInput, b: _Builder) -> None:
    if deal.customer_concentration_pct is not None and deal.customer_concentration_pct > 60:
        b.add(-10, "customer_concentration_severe")


def _score_tib(deal: ScoreInput, b: _Builder) -> None:
    if deal.time_in_business_months is None:
        b.soft_concerns.append("missing_time_in_business")
        return
    tib = deal.time_in_business_months
    if tib >= 60:
        b.add(10, "tib_5yr+")
    elif tib >= 36:
        b.add(7, "tib_3_5yr")
    elif tib >= 24:
        b.add(5, "tib_2_3yr")
    elif tib >= 12:
        b.add(0, "tib_1_2yr")
    elif tib >= 6:
        b.add(-8, "tib_6_12mo")
    else:
        b.add(-15, "tib_under_6mo")


def _score_credit(deal: ScoreInput, b: _Builder) -> None:
    if deal.credit_score is None:
        b.soft_concerns.append("missing_credit_score")
        return
    cs = deal.credit_score
    if cs >= 750:
        b.add(10, "credit_excellent")
    elif cs >= 700:
        b.add(7, "credit_strong")
    elif cs >= 650:
        b.add(3, "credit_good")
    elif cs >= 600:
        b.add(-2, "credit_fair")
    elif cs >= 550:
        b.add(-8, "credit_weak")
    else:
        b.add(-15, "credit_poor")


def _score_renewal(deal: ScoreInput, b: _Builder) -> None:
    if not deal.is_renewal:
        return
    if deal.prior_payoff_performance == "early":
        b.add(15, "renewal_early_payoff")
    elif deal.prior_payoff_performance == "on_time":
        b.add(8, "renewal_on_time_payoff")
    elif deal.prior_payoff_performance == "late":
        b.add(-5, "renewal_late_payoff")
    if deal.prior_advance_count >= 3:
        b.add(5, "renewal_loyal_merchant")


def _score_fraud_soft(deal: ScoreInput, b: _Builder) -> None:
    if deal.fraud_score > 50:
        b.add(-15, "moderate_fraud")
    elif deal.fraud_score > 30:
        b.add(-8, "low_fraud_signals")
    if deal.returned_ach_count > 3:
        b.add(-8, "returned_ach_concern")


def _score_data_quality(deal: ScoreInput, b: _Builder) -> None:
    # validation_passed=false is a hard decline; this only fires for a
    # passing-but-low-confidence extraction.
    if deal.extraction_confidence < 80:
        b.add(-5, "low_extraction_confidence")


def _score_revenue_trend(deal: ScoreInput, b: _Builder) -> None:
    """Last-3-month revenue trajectory. Declining 15%+ or growing 10%+."""
    if len(deal.monthly_breakdown) < 3:
        return
    last3 = deal.monthly_breakdown[-3:]
    first = last3[0].deposits
    last = last3[-1].deposits
    if first <= 0:
        return
    trend = (last - first) / first
    if trend <= Decimal("-0.15"):
        b.add(-15, "revenue_declining_15pct+")
    elif trend >= Decimal("0.10"):
        b.add(8, "revenue_growing_10pct+")


def _score_revenue_volatility(deal: ScoreInput, b: _Builder) -> None:
    """Coefficient of variation across 4+ months. > 0.50 high, > 0.35 moderate, ≤ 0.20 stable."""
    if len(deal.monthly_breakdown) < 4:
        return
    deposits = [float(m.deposits) for m in deal.monthly_breakdown]
    mean = statistics.mean(deposits)
    if mean <= 0:
        return
    variance = statistics.pvariance(deposits)
    cv = (variance**0.5) / mean
    if cv > 0.50:
        b.add(-12, "high_revenue_volatility")
    elif cv > 0.35:
        b.add(-6, "moderate_revenue_volatility")
    elif cv <= 0.20:
        b.add(5, "stable_revenue")


def _score_dscr(deal: ScoreInput, b: _Builder) -> None:
    """DSCR soft scoring. Hard decline (<1.0) is checked separately."""
    dscr = _dscr(deal)
    if dscr is None:
        return
    if dscr >= DSCR_STRONG:
        b.add(12, "dscr_strong")
    elif dscr >= DSCR_ADEQUATE:
        b.add(5, "dscr_adequate")
    elif dscr >= DSCR_HARD_DECLINE:
        b.add(-15, "dscr_tight")


def _dscr(deal: ScoreInput) -> Decimal | None:
    """Debt-Service Coverage Ratio. Returns None when DSCR inputs missing."""
    if deal.total_monthly_obligations is None or deal.proposed_daily_payment is None:
        return None
    obligations = deal.total_monthly_obligations + (deal.proposed_daily_payment * Decimal("22"))
    if obligations <= 0:
        return None
    return (deal.monthly_revenue / obligations).quantize(Decimal("0.01"))


# -- tier + advance + payback ------------------------------------------------


def _tier_for(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    if score >= 35:
        return "D"
    return "F"


def _recommendation_for(tier: str) -> str:
    return "decline" if tier == "F" else "approve" if tier in {"A", "B"} else "refer"


def _suggested_max_advance(monthly_revenue: Decimal, tier: str) -> Decimal:
    multiple = {
        "A": Decimal("1.5"),
        "B": Decimal("1.2"),
        "C": Decimal("1.0"),
        "D": Decimal("0.6"),
        "F": Decimal("0.0"),
    }[tier]
    raw = monthly_revenue * multiple
    # round to nearest $1,000
    return (raw / Decimal("1000")).quantize(Decimal("1")) * Decimal("1000")


def _estimated_payback_days(
    deal: ScoreInput,
    tier: str,
    suggested_max_advance: Decimal,
) -> int:
    """TS bug fix: payback = TOTAL_REPAYMENT / daily_payment, not principal.

    daily_payment = daily_revenue * holdback_pct
    total_repayment = principal * factor
    estimated_payback_days = total_repayment / daily_payment

    The TS code computed principal / daily_payment, which silently undercounts
    payback by the factor margin (≈18% for a 1.18 factor, ≈45% for a 1.45).
    Funders rely on this to set holdback expectations; getting it wrong leads
    to broken term proposals and last-minute renegotiations.
    """
    factor = _FACTOR_BY_TIER[tier]
    holdback = _HOLDBACK_BY_TIER[tier]
    if factor == 0 or holdback == 0:
        return 0
    daily_revenue = safe_divide(deal.monthly_revenue, _BUSINESS_DAYS_PER_MONTH)
    daily_payment = (daily_revenue * holdback).quantize(Decimal("0.01"))
    if daily_payment == 0:
        return 0
    total_repayment = (suggested_max_advance * factor).quantize(Decimal("0.01"))
    days = total_repayment / daily_payment
    return int(days.to_integral_value())


__all__ = [
    "DAYS_NEGATIVE_HARD_DECLINE",
    "FRAUD_SCORE_HARD_DECLINE",
    "MAX_DEBT_TO_REVENUE",
    "MCA_POSITIONS_HARD_DECLINE",
    "MIN_MONTHLY_REVENUE",
    "NSF_COUNT_HARD_DECLINE",
    "RETURNED_ACH_HARD_DECLINE",
    "TIB_MIN_MONTHS",
    "score_deal",
]
