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
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from aegis.compliance.apr import APRCalculationError, calculate_apr
from aegis.config import get_settings
from aegis.money import safe_divide
from aegis.scoring.models import PaperGrade, ScoreInput, ScoreResult
from aegis.scoring.ofac import OFACClient

# R4.4 — Known-seasonal NAICS prefixes. Conservative, prefix-match. None
# returns False. The list captures industries where multi-month deposit
# CV > 0.50 is an expected business pattern (winter snow-removal, summer
# landscaping, holiday retail, event catering, harvest-cycle agriculture)
# rather than the revenue-volatility risk the existing penalty was built
# to flag. Shadow-only: existing -12 ``high_revenue_volatility`` deduction
# still fires; the shadow flag tells the operator "this would have been
# recategorized as seasonal under a future config flip."
_SEASONAL_NAICS_PREFIXES: Final[tuple[str, ...]] = (
    "1113",   # Fruit and tree-nut farming (strongly seasonal harvest)
    "1133",   # Logging (seasonal access / weather constraints)
    "4413",   # Auto parts / tire dealers (tire + maintenance seasonality)
    "451",    # Sporting goods / hobby / book / music stores (holiday seasonal)
    "45112",  # Sporting goods stores (explicit overlap with 451)
    "488210", # Support activities for rail transportation — snow removal often coded here
    "56172",  # Janitorial — services to buildings (seasonal yard work overlap)
    "56173",  # Landscaping services (canonical seasonal industry)
    "7223",   # Special food services — event-seasonal catering
    "7225",   # Restaurants and other eating places (seasonal patterns common)
    "71390",  # Other amusement and recreation industries
    "71391",  # Golf courses and country clubs
)
# R4.4 CV thresholds. ``> 0.50`` is the existing high-volatility floor;
# ``> 1.0`` is "even for seasonal businesses, this is too extreme — keep
# flagging it for review."
_SEASONAL_CV_FLOOR: Final[Decimal] = Decimal("0.50")
_SEASONAL_CV_CEILING: Final[Decimal] = Decimal("1.0")

# DSCR thresholds. dscr = monthly_revenue / total_monthly_obligations,
# where total_monthly_obligations includes proposed_daily_payment * 22.
# cite: docs/AEGIS_MASTER_PLAN.md §5.6 — "DSCR <1.0: can't service.
# 1.0-1.25: tight. >1.25: healthy. Banks want 1.25-1.35x; MCAs more
# flexible." 1.50 picked as the MCA-flexible "strong" floor; 1.15 the
# "adequate" floor between MCA-tight and bank-healthy bands.
DSCR_HARD_DECLINE: Final[Decimal] = Decimal("1.00")
DSCR_STRONG: Final[Decimal] = Decimal("1.50")
DSCR_ADEQUATE: Final[Decimal] = Decimal("1.15")

# Hard-decline thresholds. Read from here and config — never from elsewhere.
# cite: docs/AEGIS_MASTER_PLAN.md §5.4 — "MCA debt-to-revenue >1.0-1.5x
# = stacking-spiral, responsible funders cap here." 0.40 is a conservative
# pre-stacking-spiral floor — flags merchants already at 40% obligated
# rather than waiting for the >100% spiral. cite: TODO operator validation —
# tightened from master-plan band; revisit if too aggressive on real corpus.
MAX_DEBT_TO_REVENUE: Final[Decimal] = Decimal("0.40")
# cite: docs/AEGIS_MASTER_PLAN.md §5.1 — "Average monthly true revenue
# Min $10-15k major funders; $8k mid; $5k specialty." $10k is the major-
# funder floor; below this no funder in the broker's network will accept.
MIN_MONTHLY_REVENUE: Final[Decimal] = Decimal("10000.00")
# cite: docs/AEGIS_MASTER_PLAN.md §7 "Composite fraud score (0-100, per
# Ocrolus Detect): ≤30 highly suspicious — usually reject; 31-60 review
# required; 61-100 low concern." AEGIS scores in the OPPOSITE direction
# (higher = more fraud), so 70 = "highly suspicious" boundary inverted.
FRAUD_SCORE_HARD_DECLINE: Final[int] = 70
# cite: docs/AEGIS_MASTER_PLAN.md §5.2 — "Days negative 0-4: ok. 5-9:
# yellow. ≥10: usually decline." 15 is the conservative hard-decline
# threshold (beyond "usually decline") to leave headroom for soft-scoring
# the 5-9 + 10-14 bands separately.
DAYS_NEGATIVE_HARD_DECLINE: Final[int] = 15
# cite: docs/AEGIS_MASTER_PLAN.md §5.3 — "NSF count / month: 0-2 ok.
# 3-5 yellow + compensating factors. 6-10 usually decline. >10 auto-decline."
# 10 is the auto-decline floor.
NSF_COUNT_HARD_DECLINE: Final[int] = 10
# cite: docs/AEGIS_MASTER_PLAN.md §5.3 — "Returned ACH treated as
# NSF-equivalent." cite: TODO operator validation — 5 chosen as the
# conservative half-of-NSF-decline threshold (returned ACH is a stronger
# signal of broken counterparty trust than soft NSF); revisit after corpus.
RETURNED_ACH_HARD_DECLINE: Final[int] = 5
# cite: docs/AEGIS_MASTER_PLAN.md §5.5 — "Time in business <6mo:
# auto-decline most. 6-12mo: startup only, expensive." 3 is the absolute
# floor below which no funder in the network will price a deal.
TIB_MIN_MONTHS: Final[int] = 3
# cite: docs/AEGIS_MASTER_PLAN.md §5.4 — "Active MCA positions: 0 clean,
# 1 2nd-pos market, 2 limited, 3+ most decline." > 2 active = decline.
MCA_POSITIONS_HARD_DECLINE: Final[int] = 2  # > 2 active = decline

# Tier-based factor / holdback. AEGIS uses Decimal throughout.
# cite: docs/AEGIS_MASTER_PLAN.md §5.8 paper grades — A 1.15-1.25,
# B 1.25-1.35, C 1.35-1.45, D 1.45-1.55. AEGIS picks mid-band values
# (1.18, 1.29, 1.35, 1.45) as the default factor per internal tier so
# the broker can negotiate ±$0.05 with the funder without re-tiering.
_FACTOR_BY_TIER: Final[dict[str, Decimal]] = {
    "A": Decimal("1.18"),
    "B": Decimal("1.29"),
    "C": Decimal("1.35"),
    "D": Decimal("1.45"),
    "F": Decimal("0.00"),
}
# cite: TODO operator validation — holdback envelope (10/12/15/20%)
# chosen by V1 calibration on synthetic corpus; industry-standard MCA
# holdbacks run 8-22%, but master plan does not lock per-tier values.
# Revisit after R4.5 POD backtest.
_HOLDBACK_BY_TIER: Final[dict[str, Decimal]] = {
    "A": Decimal("0.10"),
    "B": Decimal("0.12"),
    "C": Decimal("0.15"),
    "D": Decimal("0.20"),
    "F": Decimal("0.00"),
}
# cite: industry convention — 22 business days/month is the MCA payback
# calendar (5 days/week x ~4.4 weeks). Used for daily_payment derivation
# across the scorer and per-funder match.
_BUSINESS_DAYS_PER_MONTH: Final[Decimal] = Decimal("22")


@dataclass
class _Builder:
    score: int = 50
    flags: list[str] = None  # type: ignore[assignment]
    breakdown: list[dict[str, object]] = None  # type: ignore[assignment]
    soft_concerns: list[str] = None  # type: ignore[assignment]
    shadow_flags: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.flags = []
        self.breakdown = []
        self.soft_concerns = []
        self.shadow_flags = []

    def add(self, delta: int, factor: str) -> None:
        self.score += delta
        self.breakdown.append({"factor": factor, "delta": delta})


def score_deal(
    deal: ScoreInput,
    *,
    ofac: OFACClient | None = None,
) -> ScoreResult:
    """Score a deal. Hard declines first; soft scoring + tier/payback after."""
    hard_declines, decline_details, shadow_flags = _check_hard_declines(deal, ofac)
    # R3.4 — state-by-state enforcement signals. Shadow-only; fires on both
    # the hard-decline path and the soft-score path so the operator sees
    # the concern regardless of how the deal exits the scorer.
    shadow_flags.extend(
        _state_disclosure_flag(
            merchant_state=deal.state,
            deal_state=deal.state,
            advance_fees_charged=deal.advance_fees_charged,
        )
    )
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
            decline_details=decline_details,
            paper_grade="D",
            paper_grade_reasons=["hard_decline"],
            shadow_flags=shadow_flags,
        )

    return _soft_score(deal, initial_shadow_flags=shadow_flags)


# -- hard declines -----------------------------------------------------------


def _check_hard_declines(
    deal: ScoreInput,
    ofac: OFACClient | None,
) -> tuple[list[str], dict[str, list[dict[str, str]]], list[str]]:
    """Return ``(reasons, details, shadow_flags)``.

    ``details`` carries structured payloads for reasons that need
    downstream audit / reporting — currently only ``ofac_matches`` (the
    SDN candidate name + uid that fired, per
    ``docs/compliance/07_ofac_sanctions.md`` §"reporting workflow").
    The endpoint forwards this into ``audit_log`` so the operator can
    disposition + file the 10-business-day Initial Report of Blocked
    Property without re-running the screen.

    ``shadow_flags`` carries decision-boundary annotations that don't
    change the existing decline behavior — see ``ScoreResult.shadow_flags``
    docstring + CLAUDE.md "Decision-boundary changes — shadow-first".
    """
    reasons: list[str] = []
    details: dict[str, list[dict[str, str]]] = {}
    shadow_flags: list[str] = []

    if ofac is not None:
        # OFAC screening — checked FIRST so a sanctioned merchant cannot
        # accidentally get other reasons reported and ignored.
        ofac_matches: list[dict[str, str]] = []
        for input_field, value in (
            ("business_name", deal.business_name),
            ("owner_name", deal.owner_name),
        ):
            match = ofac.find_match(value)
            if match is None:
                continue
            ofac_matches.append(
                {
                    "input_field": input_field,
                    "matched_name": match.matched_name,
                    "sdn_uid": match.sdn_uid or "",
                }
            )
        if ofac_matches:
            reasons.append("ofac_sanctions_match")
            details["ofac_matches"] = ofac_matches

    if deal.mca_positions > MCA_POSITIONS_HARD_DECLINE:
        reasons.append(f"stacking_exceeds_limit: {deal.mca_positions} active positions")

    if deal.debt_to_revenue > MAX_DEBT_TO_REVENUE:
        reasons.append(
            f"debt_to_revenue_exceeds_40pct: {(deal.debt_to_revenue * 100):.0f}%"
        )

    if deal.fraud_score >= FRAUD_SCORE_HARD_DECLINE:
        reasons.append(f"fraud_score_critical: {deal.fraud_score}")

    eof_threshold = get_settings().aegis_eof_threshold
    if deal.eof_markers > eof_threshold:
        reasons.append(f"incremental_pdf_saves: {deal.eof_markers} EOF markers")
        # AUDIT R4.6: scorer hard-declines at ``> aegis_eof_threshold`` EOFs;
        # pipeline policy (docs/AUDIT_2026_05_10.md line 46) is "2 EOFs →
        # review, 3+ → manual_review". The threshold is now an env var
        # (AEGIS_EOF_THRESHOLD). Per CLAUDE.md "Decision-boundary changes —
        # shadow-first": Reconcile by setting AEGIS_EOF_THRESHOLD=2 in
        # /etc/aegis/aegis.env after corpus validation. The shadow flag
        # documents the CURRENT active policy so the operator can audit
        # which posture is live without grepping config.
        if eof_threshold == 1:
            # Default: legacy behavior. Scorer declines at 2+, pipeline
            # routes 2 → review. Mismatch remains until the flip.
            shadow_flags.append(
                "eof_policy_mismatch:scorer_declines_at_2_pipeline_routes_review"
            )
        else:
            # Lifted: scorer aligns with pipeline policy. Flag confirms
            # the lift so the operator sees which threshold is active.
            shadow_flags.append(
                f"eof_policy_aligned:scorer_declines_at_{eof_threshold + 1}"
                f"_threshold={eof_threshold}"
            )

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

    # Phase 9 hard-decline rules (master plan §19 task 3).
    if deal.acceleration_clause_triggered:
        reasons.append("acceleration_clause_triggered")
    if deal.unauthorized_withdrawal_dispute:
        reasons.append("unauthorized_withdrawal_dispute_active")
    if deal.tampering_confirmed:
        reasons.append("bank_statement_tampering_confirmed")

    return reasons, details, shadow_flags


# -- shadow-mode helpers (R3.4, R4.4, R4.6) ---------------------------------


def _is_seasonal_industry(naics: str | None) -> bool:
    """R4.4 — True if ``naics`` prefix-matches a known-seasonal industry.

    NAICS is matched by prefix so 6-digit codes like ``561730`` map to
    ``56173`` (landscaping). ``None`` returns False — we never assume
    seasonality without an explicit industry code. Conservative list per
    audit R4.4 spec; expand only with operator + corpus validation.
    """
    if naics is None:
        return False
    return any(naics.startswith(prefix) for prefix in _SEASONAL_NAICS_PREFIXES)


def _tib_ramp_shadow_flag(months: int | None) -> str | None:
    """H8 — graduated TIB penalty shadow flag.

    Today: ``_score_tib`` applies -15 to anything < 6mo (after the
    < 3mo hard decline), and -8 to 6-11mo. A 3.1-month business gets
    the same penalty as a 5.9-month business. Crude.

    This helper computes what a graduated penalty WOULD be:
      <6mo   → -15 (matches today's floor)
      6-11   → -10
      12-17  → -5
      18-23  → -2
      >=24   → 0

    Returns a single-line flag ``tib_ramp_shadow:months=N_current_delta=
    -X_graduated_delta=-Y`` or ``None`` when no shadow annotation is
    useful (months is None, hard-decline territory <3, or current
    bracket already grants 0 credit). The existing -15 / -8 / 0 deltas
    in ``_score_tib`` are NOT modified — this is annotation only.
    """
    if months is None:
        return None
    if months < TIB_MIN_MONTHS:
        # Hard-decline territory — nothing to ramp.
        return None

    # Current deltas applied by _score_tib (for >=3mo path):
    if months < 6:
        current_delta = -15
    elif months < 12:
        current_delta = -8
    elif months < 24:
        current_delta = 0
    elif months < 36:
        current_delta = 5
    elif months < 60:
        current_delta = 7
    else:
        current_delta = 10

    # Graduated ramp candidate (only penalty bands — shadow does not
    # propose changing the positive credits for 24+/36+/60+).
    if months < 6:
        graduated_delta = -15
    elif months < 12:
        graduated_delta = -10
    elif months < 18:
        graduated_delta = -5
    elif months < 24:
        graduated_delta = -2
    else:
        # 24+ months — graduated ramp converges to today's positive
        # credits; no shadow annotation needed.
        return None

    return (
        f"tib_ramp_shadow:months={months}"
        f"_current_delta={current_delta}"
        f"_graduated_delta={graduated_delta}"
    )


def _state_disclosure_flag(
    merchant_state: str | None,
    deal_state: str | None,
    advance_fees_charged: bool | None,
) -> list[str]:
    """R3.4 — state-by-state enforcement signals (shadow only).

    Returns the list of shadow flags to surface for the operator. Never
    promotes to ``hard_decline_reasons`` or alters tier / recommendation.

    - **TX HB 700** — Texas Finance Code Ch. 398 requires a first-priority
      lien on the merchant's accounts for MCAs; standard ACH-debit
      structures don't satisfy it. AEGIS keeps TX in ``state_not_served``
      (see ``src/aegis/compliance/states.py`` docstring lines 44-49 +
      line 989-991, and ``docs/counsel/phase-4-questions.md``), but a
      TX merchant can still arrive at the scorer through edge paths
      (renewals, override). Emit a review hint.
    - **FL / GA advance-fee prohibition** — both states' broker statutes
      prohibit charging the merchant a fee in advance of funding (per
      ``compliance/states.py`` ``broker_advance_fees_prohibited=True``
      at lines 724 / 816). When the deal flags advance fees, surface
      the concern.
    """
    flags: list[str] = []
    state = (merchant_state or "").upper()
    if state == "TX":
        flags.append("state_enforcement_concern:TX_HB700_tx_merchant_review")
    if state in {"FL", "GA"} and advance_fees_charged is True:
        flags.append("state_enforcement_concern:FL_GA_advance_fee_prohibition")
    return flags


# -- soft scoring ------------------------------------------------------------


def _soft_score(
    deal: ScoreInput,
    *,
    initial_shadow_flags: list[str] | None = None,
) -> ScoreResult:
    b = _Builder()
    if initial_shadow_flags:
        b.shadow_flags.extend(initial_shadow_flags)

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
    _score_counterparty_concentration(deal, b)
    _score_payroll_present(deal, b)
    _score_ai_generated(deal, b)

    # H8 — TIB ramp shadow flag. Annotates what a graduated TIB penalty
    # WOULD do without altering the existing _score_tib bands. Per
    # CLAUDE.md "Decision-boundary changes — shadow-first": validate
    # against the corpus before flipping severity via config.
    tib_shadow = _tib_ramp_shadow_flag(deal.time_in_business_months)
    if tib_shadow is not None:
        b.shadow_flags.append(tib_shadow)

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

    paper_grade, paper_reasons = compute_paper_grade(deal)

    # M6 — deal-level APR via Reg Z Appendix J actuarial method. Mirrors
    # the per-funder pattern in scoring/match_funders._estimate_apr, but
    # uses the deal-level recommended factor / holdback / payback. APR is
    # an output-only field (nothing reads from it for tier / recommendation
    # logic) so populating it is gap-close, not a decision-boundary change.
    # On APRCalculationError → apr=None + soft_concern. NEVER substitute
    # 0.00% (R0.4 lie discipline / docs/AUDIT_2026_05_10.md H7).
    apr = _compute_deal_apr(
        suggested_max_advance=suggested_max,
        recommended_factor_rate=factor,
        recommended_holdback_pct=holdback,
        estimated_payback_days=payback_days,
    )
    if apr is None and _apr_inputs_present(
        suggested_max, factor, holdback, payback_days
    ):
        soft_concerns.append(
            "apr_not_computable: optimizer could not bracket a root for "
            "the recommended terms"
        )

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
        apr=apr,
        paper_grade=paper_grade,
        paper_grade_reasons=paper_reasons,
        shadow_flags=list(b.shadow_flags),
    )


def _apr_inputs_present(
    suggested_max: Decimal,
    factor: Decimal,
    holdback: Decimal,
    payback_days: int,
) -> bool:
    """All four scoring outputs needed to synthesize an APR payment stream.

    None of these are nullable by type, but in practice the hard-decline
    short-circuit returns zeros and the soft-score path may converge on
    a zero advance. Only fire the soft_concern when APR *should* have
    been computable but wasn't.
    """
    return (
        suggested_max > 0
        and factor > Decimal("1.0")
        and holdback > 0
        and payback_days > 0
    )


def _compute_deal_apr(
    *,
    suggested_max_advance: Decimal,
    recommended_factor_rate: Decimal,
    recommended_holdback_pct: Decimal,
    estimated_payback_days: int,
) -> Decimal | None:
    """Synthesize a daily payment stream from the deal-level recommendation
    and run the actuarial APR. Returns ``None`` on degenerate inputs or
    when the optimizer cannot bracket a root.

    Mirrors ``scoring.match_funders._estimate_apr`` (per-funder) but uses
    the deal-level recommended terms, so the dossier can show a single
    "AEGIS-recommended APR" alongside the per-funder grid.

    The disbursement date is anchored arbitrarily — APR depends only on
    day offsets, not calendar position (Reg Z Appendix J).
    """
    if not _apr_inputs_present(
        suggested_max_advance,
        recommended_factor_rate,
        recommended_holdback_pct,
        estimated_payback_days,
    ):
        return None

    total_repayment = (
        suggested_max_advance * recommended_factor_rate
    ).quantize(Decimal("0.01"))
    daily_payment = (
        total_repayment / Decimal(estimated_payback_days)
    ).quantize(Decimal("0.01"))
    if daily_payment <= 0:
        return None

    # Arbitrary anchor — APR depends only on offsets from this date.
    disbursement = date(2026, 1, 1)
    payments = [
        (disbursement + timedelta(days=offset), daily_payment)
        for offset in range(1, estimated_payback_days + 1)
    ]
    try:
        apr = calculate_apr(suggested_max_advance, payments, disbursement)
    except APRCalculationError:
        # No silent zero (R0.4 / H7) — surface as None and let the
        # caller append an apr_not_computable soft_concern.
        return None
    # Quantize to 4 decimal places to match the snapshot.apr_calculated
    # Pydantic constraint (`max_digits=8, decimal_places=4`) — calculate_apr
    # returns arbitrary brentq precision (e.g. Decimal("0.318887...")).
    return apr.quantize(Decimal("0.0001"))


# -- soft scoring components -------------------------------------------------


def _score_revenue(deal: ScoreInput, b: _Builder) -> None:
    # cite: docs/AEGIS_MASTER_PLAN.md §5.1 + §5.8 — revenue bands match
    # paper-grade thresholds (A: $25k+, B: $15k+, C: $10k+). $50k+ and
    # $100k+ tiers above add credit for prime-funder appetite; $10-15k
    # band is below the major-funder floor and gets a small penalty.
    # cite: TODO operator validation — band deltas (+25/+20/+12/+5/-5)
    # are V1 calibration weights, revisit after R4.5 POD backtest.
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
    # cite: docs/AEGIS_MASTER_PLAN.md §5.2 — "ADB ≥10-15x expected daily
    # MCA payment = ≥10-15% of monthly revenue." $15k+ corresponds to
    # an A-paper ADB at $100k revenue; $1k floor maps to "Ending balance
    # consistently <$1k = tight cash flow."
    # cite: docs/AEGIS_MASTER_PLAN.md §5.2 — "Days negative 0-4: ok.
    # 5-9: yellow. ≥10: usually decline." > 5 chronic_negative aligns
    # with the yellow-band threshold (hard-decline at 15+ is separate).
    # cite: TODO operator validation — band deltas are V1 calibration.
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
    # cite: docs/AEGIS_MASTER_PLAN.md §5.3 — "NSF count/month: 0-2 ok,
    # 3-5 yellow + compensating factors, 6-10 usually decline, >10 auto-
    # decline." Rate-normalized (per 30-day window) to handle short or
    # truncated statement periods correctly. -22 at the auto-decline
    # boundary (still below the hard-decline rule), -14 at usually-
    # decline, -7 at yellow, +8 for zero-NSF prime signal.
    # cite: TODO operator validation — exact deltas are V1 calibration.
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
    # cite: docs/AEGIS_MASTER_PLAN.md §5.7 — "Payroll cadence: regular
    # biweekly/weekly = real operating business. Missing on $50k/mo =
    # red flag." Also §6.4 detector ``payroll_absent`` — "No ACH to/from
    # ADP/Gusto/Paychex/Rippling/Square Payroll over period (soft signal)."
    # $50k cutoff matches the master-plan red-flag threshold.
    # cite: TODO operator validation — +8/-5 deltas are V1 calibration.
    if deal.payroll_detected:
        b.add(8, "payroll_detected")
    elif deal.monthly_revenue > 50_000:
        b.add(-5, "no_payroll_high_revenue")


def _score_stacking(deal: ScoreInput, b: _Builder) -> None:
    # cite: docs/AEGIS_MASTER_PLAN.md §5.4 — "Active MCA positions:
    # 0 clean, 1 2nd-pos market, 2 limited, 3+ most decline." The
    # +10/-8/-18 ladder reflects market appetite: clean = prime, one
    # position = mainstream-tightened, two = sub-prime-only.
    # cite: TODO operator validation — exact deltas are V1 calibration.
    if deal.mca_positions == 0:
        b.add(10, "clean_position")
    elif deal.mca_positions == 1:
        b.add(-8, "one_position")
    elif deal.mca_positions == 2:
        b.add(-18, "double_stacked")


def _score_industry(deal: ScoreInput, b: _Builder) -> None:
    # cite: docs/AEGIS_MASTER_PLAN.md §5.5 — "Industry NAICS Restricted:
    # cannabis, adult, firearms, crypto, gambling, MLM, debt-relief,
    # MSB." ``avoid`` tier is the hard-decline path (delta 0 here, the
    # hard-decline rule catches it upstream); low/moderate/elevated/high
    # are V1 calibration bands.
    # cite: TODO operator validation — deltas are V1 calibration.
    if deal.industry_risk_tier is None:
        return
    delta = {"low": 10, "moderate": 5, "elevated": -3, "high": -10, "avoid": 0}[
        deal.industry_risk_tier
    ]
    b.add(delta, f"industry_{deal.industry_risk_tier}")


def _score_concentration(deal: ScoreInput, b: _Builder) -> None:
    # cite: docs/AEGIS_MASTER_PLAN.md §5.1 — "Largest deposit % of
    # revenue: >30% from one source = customer concentration." Master
    # plan flags >30% as the concern threshold; the legacy field uses a
    # more aggressive >60% floor because customer_concentration_pct is
    # operator-entered (memory-derived), not statement-derived. The
    # newer statement-derived signal lives in
    # ``_score_counterparty_concentration``.
    if deal.customer_concentration_pct is not None and deal.customer_concentration_pct > 60:
        b.add(-10, "customer_concentration_severe")


def _score_tib(deal: ScoreInput, b: _Builder) -> None:
    # cite: docs/AEGIS_MASTER_PLAN.md §5.5 — "Time in business <6mo:
    # auto-decline most. 6-12mo: startup only, expensive. 12-24mo:
    # standard. 24+: best." Paper-grade thresholds (§5.8) also use 24mo
    # for A-paper and 12mo for B-paper. 5yr+ gets the top credit as a
    # "fully seasoned" business.
    # cite: TODO operator validation — exact deltas are V1 calibration.
    # H8 shadow flag below recommends graduating the -15 floor.
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
    # cite: docs/AEGIS_MASTER_PLAN.md §5.8 — "A paper: FICO 650+." 650
    # is the prime/sub-prime FICO boundary widely used across MCA
    # funders. 750+/700+ match standard consumer-credit "excellent"/
    # "strong" boundaries. <600 is sub-prime; <550 is deep-sub-prime.
    # cite: TODO operator validation — exact deltas are V1 calibration.
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
    # cite: docs/AEGIS_MASTER_PLAN.md §6.4 — renewal context (early/
    # on_time/late/default payoff) drives the "is this merchant a known
    # quantity" weighting. A ``default`` payoff is hard-decline upstream;
    # late is recoverable but penalized. ``prior_advance_count >= 3``
    # captures the "loyal renewal merchant" pattern funders prize.
    # cite: TODO operator validation — exact deltas are V1 calibration.
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
    # cite: docs/AEGIS_MASTER_PLAN.md §7 — "Composite fraud score:
    # ≤30 highly suspicious; 31-60 review required; 61-100 low concern."
    # AEGIS scores in the OPPOSITE direction (higher = more fraud).
    # Inverted: >50 = "review required" mid-band, >30 = "low concern"
    # entry boundary. Hard-decline at 70 catches >50 of inverted scale.
    # cite: docs/AEGIS_MASTER_PLAN.md §5.3 — "Returned ACH treated as
    # NSF-equivalent." >3 mirrors mid-band NSF concern; >5 is hard-decline.
    # cite: TODO operator validation — exact deltas are V1 calibration.
    if deal.fraud_score > 50:
        b.add(-15, "moderate_fraud")
    elif deal.fraud_score > 30:
        b.add(-8, "low_fraud_signals")
    if deal.returned_ach_count > 3:
        b.add(-8, "returned_ach_concern")


def _score_data_quality(deal: ScoreInput, b: _Builder) -> None:
    # validation_passed=false is a hard decline; this only fires for a
    # passing-but-low-confidence extraction.
    # cite: TODO operator validation — 80 confidence floor + -5 penalty
    # chosen by V1 calibration. The hard floor is enforced via the
    # validation_passed hard decline; this soft band tags "passed-but-
    # noisy" extractions for operator review without changing the gate.
    if deal.extraction_confidence < 80:
        b.add(-5, "low_extraction_confidence")


def _score_counterparty_concentration(deal: ScoreInput, b: _Builder) -> None:
    """Concentration penalties from counterparty signals.

    Distinct from the legacy ``customer_concentration_pct`` field
    (operator-entered). The new ``top_counterparty_pct`` is computed by
    ``patterns.CounterpartySignals`` directly from the bank statement.
    Both are scored if present — they can disagree, in which case the
    statement-derived signal is generally more reliable than memory.
    """
    # cite: docs/AEGIS_MASTER_PLAN.md §5.1 + §5.7 — ">30% from one
    # source = customer concentration" (§5.1), ">40% from one = single-
    # customer dependency" (§5.7 Top 5 counterparties). 30/40/60 ladder
    # maps to escalating severity from §5.1 concern (-3) to §5.7 single-
    # customer dependency (-6) to severe concentration (-12 at 60%+).
    # cite: docs/AEGIS_MASTER_PLAN.md §5.7 — top-5 counterparty signals
    # are master-plan listed; 80% top-5 concentration is the operator-
    # validated severe concentration floor.
    # cite: TODO operator validation — exact deltas are V1 calibration.
    top = deal.top_counterparty_pct
    if top is not None:
        if top >= 60:
            b.add(-12, "top_counterparty_60pct+")
        elif top >= 40:
            b.add(-6, "top_counterparty_40_60pct")
        elif top >= 30:
            b.add(-3, "top_counterparty_30_40pct")

    top5 = deal.top_5_revenue_share_pct
    if top5 is not None and top5 >= 80:
        b.add(-4, "top_5_revenue_share_80pct+")
    top5_exp = deal.top_5_expense_share_pct
    if top5_exp is not None and top5_exp >= 80:
        # Concentrated expense side is informational; a small soft note.
        b.soft_concerns.append(
            f"top_5_expense_concentration_{top5_exp}pct"
        )


def _score_payroll_present(deal: ScoreInput, b: _Builder) -> None:
    """New payroll signal layered with the legacy ``payroll_detected``.

    Both pass through the same +8 / -5 grid as ``_score_payroll`` to
    keep the score envelope stable, but only ``payroll_present`` (the
    detector-derived signal) is decremented for high-revenue businesses
    with no payroll. The legacy field is kept untouched so test
    fixtures that set ``payroll_detected`` still receive a credit.
    """
    # cite: docs/AEGIS_MASTER_PLAN.md §5.7 + §6.4 — "Payroll cadence:
    # regular biweekly/weekly = real operating business" and
    # ``payroll_absent`` detector. $50k cutoff matches the master-plan
    # red-flag threshold mirrored from _score_payroll.
    # cite: TODO operator validation — +4 detector-confidence credit is
    # V1 calibration; half of the legacy +8 to avoid double-counting.
    if deal.payroll_present and not deal.payroll_detected:
        # New signal credits when the detector found payroll the
        # operator hadn't flagged on the merchant row.
        b.add(4, "payroll_present_detector")
    if not deal.payroll_present and not deal.payroll_detected:
        if deal.monthly_revenue >= Decimal("50000.00"):
            # Mark as a soft concern only — the legacy _score_payroll
            # already adds the -5 penalty, we don't want to double-tax.
            b.soft_concerns.append("payroll_absent_high_revenue")


def _score_ai_generated(deal: ScoreInput, b: _Builder) -> None:
    """Composite AI-generated-statement score.

    Per master plan §6.4, AI-generated fakes should be *scored*, never
    auto-declined. Thresholds: >=85 = strong signal (-15), >=70 = medium
    (-8), >=55 = weak (-3). Below 55 is no signal.
    """
    # cite: docs/AEGIS_MASTER_PLAN.md §6.4 — ``ai_generated_statement``
    # detector: "Composite: perfect reconciliation, LLM-style descriptions
    # (full sentences, no abbreviations), generic counterparties. Score,
    # don't auto-decline." 55/70/85 ladder is the V1 calibration.
    # cite: TODO operator validation — exact thresholds and deltas are
    # V1 calibration; the master plan dictates "score, don't auto-decline"
    # but does not lock the per-band magnitude.
    score = deal.ai_generated_score
    if score >= 85:
        b.add(-15, "ai_generated_statement_strong")
    elif score >= 70:
        b.add(-8, "ai_generated_statement_medium")
    elif score >= 55:
        b.add(-3, "ai_generated_statement_weak")


def _score_revenue_trend(deal: ScoreInput, b: _Builder) -> None:
    """Last-3-month revenue trajectory. Declining 15%+ or growing 10%+."""
    # cite: docs/AEGIS_MASTER_PLAN.md §5.1 — "Revenue trend: Slope of
    # monthly true revenue. Up: factor improves 0.05-0.10. Down: tier
    # downgrade." 15% decline / 10% growth chosen as conservative
    # 3-month deltas; master plan does not lock the exact percentages.
    # cite: TODO operator validation — -15/+8 magnitudes are V1.
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
    """Coefficient of variation across 4+ months. > 0.50 high, > 0.35 moderate, ≤ 0.20 stable.

    R4.4 — shadow-mode seasonality re-categorization. When the merchant's
    NAICS prefix is in ``_SEASONAL_NAICS_PREFIXES`` and CV > 0.50, emit a
    shadow flag that records what a future seasonality-aware policy would
    have done. The existing ``-12 high_revenue_volatility`` deduction
    STILL fires — this is annotation only, not a behavior change. CV
    above 1.0 is too extreme even for seasonal businesses; emit a
    different shadow flag noting the penalty still applies as intended.
    """
    # cite: docs/AEGIS_MASTER_PLAN.md §5.1 — "Revenue volatility (CV)
    # stddev/mean of monthly true revenue. High CV ⇒ tier downgrade
    # even at same average." CV bands (0.20 stable / 0.35 moderate /
    # 0.50 high) match the SEASONAL_CV_FLOOR (0.50) used for the R4.4
    # seasonality shadow flag above.
    # cite: TODO operator validation — exact CV thresholds and deltas
    # are V1 calibration; master plan does not lock specific CV bands.
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
        # R4.4 shadow annotation — does NOT alter the -12 penalty above.
        if _is_seasonal_industry(deal.industry_naics):
            cv_decimal = Decimal(str(cv)).quantize(Decimal("0.001"))
            naics_label = deal.industry_naics or "unknown"
            if Decimal(str(cv)) <= _SEASONAL_CV_CEILING:
                b.shadow_flags.append(
                    f"seasonality_recategorized:cv={cv_decimal}_naics={naics_label}"
                    "_would_skip_volatility_penalty"
                )
            else:
                b.shadow_flags.append(
                    f"seasonality_observed_but_volatility_extreme:cv={cv_decimal}"
                    f"_naics={naics_label}_penalty_still_applied"
                )
    elif cv > 0.35:
        b.add(-6, "moderate_revenue_volatility")
    elif cv <= 0.20:
        b.add(5, "stable_revenue")


def _score_dscr(deal: ScoreInput, b: _Builder) -> None:
    """DSCR soft scoring. Hard decline (<1.0) is checked separately."""
    # cite: docs/AEGIS_MASTER_PLAN.md §5.6 — "DSCR <1.0 can't service,
    # 1.0-1.25 tight, >1.25 healthy. Banks want 1.25-1.35x; MCAs more
    # flexible." DSCR_STRONG=1.50 (above bank-healthy), DSCR_ADEQUATE=
    # 1.15 (between tight and healthy for MCA flexibility), and the
    # tight band (1.00-1.15) earns the -15 soft penalty.
    # cite: TODO operator validation — +12/+5/-15 deltas are V1.
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


# Paper-grade thresholds taken from master plan §5.8. Each grade has a
# set of criteria that must ALL be satisfied; downgrade kicks in on the
# first failed criterion. Returned alongside human-readable reason codes
# so the dossier can show "graded B because: adb_in_8_15pct_band, …".
_PAPER_GRADE_A_TIB = 24
_PAPER_GRADE_A_REV = Decimal("25000.00")
_PAPER_GRADE_A_ADB_PCT = Decimal("0.15")
_PAPER_GRADE_A_NSF = 2
_PAPER_GRADE_A_CREDIT = 650

_PAPER_GRADE_B_TIB = 12
_PAPER_GRADE_B_REV = Decimal("15000.00")
_PAPER_GRADE_B_ADB_PCT_LOW = Decimal("0.08")
_PAPER_GRADE_B_NSF = 5
_PAPER_GRADE_B_MCA = 1

_PAPER_GRADE_C_TIB = 6
_PAPER_GRADE_C_REV = Decimal("10000.00")
_PAPER_GRADE_C_NSF = 10
_PAPER_GRADE_C_MCA = 2


def compute_paper_grade(deal: ScoreInput) -> tuple[PaperGrade, list[str]]:
    """Industry-standard paper grade (master plan §5.8).

    Funder-facing classification distinct from AEGIS's internal tier.
    Returns ``(grade, reasons)`` where ``reasons`` is the list of
    criterion codes that drove the grade — paper grade is a deterministic
    waterfall: try A; on any miss, fall through to B; etc.

    Down to D for any deal that doesn't satisfy C-paper minimums. We
    do not return F here — paper grades are funder labels, not internal
    pass/fail. The internal ``tier`` carries that gate.
    """
    rev = deal.monthly_revenue
    adb = deal.avg_daily_balance
    adb_pct = (adb / rev) if rev > 0 else Decimal("0")
    nsf = deal.num_nsf
    mca = deal.mca_positions
    tib = deal.time_in_business_months or 0
    credit = deal.credit_score

    # --- Try A ----------------------------------------------------------
    reasons_a: list[str] = []
    a_ok = True
    if tib >= _PAPER_GRADE_A_TIB:
        reasons_a.append(f"tib_{_PAPER_GRADE_A_TIB}mo+")
    else:
        reasons_a.append(f"tib_below_{_PAPER_GRADE_A_TIB}mo")
        a_ok = False
    if rev >= _PAPER_GRADE_A_REV:
        reasons_a.append("revenue_25k+")
    else:
        reasons_a.append("revenue_below_25k")
        a_ok = False
    if adb_pct >= _PAPER_GRADE_A_ADB_PCT:
        reasons_a.append("adb_gte_15pct_revenue")
    else:
        reasons_a.append("adb_below_15pct_revenue")
        a_ok = False
    if nsf <= _PAPER_GRADE_A_NSF:
        reasons_a.append("nsf_0_2")
    else:
        reasons_a.append("nsf_above_2")
        a_ok = False
    if mca == 0:
        reasons_a.append("clean_position")
    else:
        reasons_a.append("has_mca_position")
        a_ok = False
    if credit is None or credit >= _PAPER_GRADE_A_CREDIT:
        reasons_a.append(
            "credit_650+" if credit is not None else "credit_unknown_pass"
        )
    else:
        reasons_a.append("credit_below_650")
        a_ok = False
    if a_ok:
        return "A", reasons_a

    # --- Try B ----------------------------------------------------------
    reasons_b: list[str] = []
    b_ok = True
    if tib >= _PAPER_GRADE_B_TIB:
        reasons_b.append(f"tib_{_PAPER_GRADE_B_TIB}mo+")
    else:
        reasons_b.append(f"tib_below_{_PAPER_GRADE_B_TIB}mo")
        b_ok = False
    if rev >= _PAPER_GRADE_B_REV:
        reasons_b.append("revenue_15k+")
    else:
        reasons_b.append("revenue_below_15k")
        b_ok = False
    if adb_pct >= _PAPER_GRADE_B_ADB_PCT_LOW:
        reasons_b.append("adb_gte_8pct_revenue")
    else:
        reasons_b.append("adb_below_8pct_revenue")
        b_ok = False
    if nsf <= _PAPER_GRADE_B_NSF:
        reasons_b.append("nsf_0_5")
    else:
        reasons_b.append("nsf_above_5")
        b_ok = False
    if mca <= _PAPER_GRADE_B_MCA:
        reasons_b.append("mca_0_1")
    else:
        reasons_b.append("mca_above_1")
        b_ok = False
    if b_ok:
        return "B", reasons_b

    # --- Try C ----------------------------------------------------------
    reasons_c: list[str] = []
    c_ok = True
    if tib >= _PAPER_GRADE_C_TIB:
        reasons_c.append(f"tib_{_PAPER_GRADE_C_TIB}mo+")
    else:
        reasons_c.append(f"tib_below_{_PAPER_GRADE_C_TIB}mo")
        c_ok = False
    if rev >= _PAPER_GRADE_C_REV:
        reasons_c.append("revenue_10k+")
    else:
        reasons_c.append("revenue_below_10k")
        c_ok = False
    if nsf <= _PAPER_GRADE_C_NSF:
        reasons_c.append("nsf_0_10")
    else:
        reasons_c.append("nsf_above_10")
        c_ok = False
    if mca <= _PAPER_GRADE_C_MCA:
        reasons_c.append("mca_0_2")
    else:
        reasons_c.append("mca_above_2")
        c_ok = False
    if c_ok:
        return "C", reasons_c

    # --- Default D ------------------------------------------------------
    return "D", reasons_c


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
    "compute_paper_grade",
    "score_deal",
]
