"""Funder matching — hard fails + soft concerns separated.

A funder match is a tuple of `(qualifies, hard_fails, soft_concerns)`.
Hard fails mean the funder will not approve regardless of relationship;
soft concerns degrade likelihood but don't reject.

TS-fix: missing data is a soft concern, not a silent pass
--------------------------------------------------------
Missing credit_score / time_in_business is a SOFT CONCERN, not a silent
pass. Scoring missing data as "no concern" is how stacking gets through.

Stacking semantics
------------------
We deliberately separate "the funder published an exact maximum" from
"the funder hasn't said". The four branches are:
  - max_positions set + deal positions > max     -> hard fail
    (`exceeds_max_positions`) — published constraint is binding.
  - accepts_stacking=False + deal positions >= 1 -> SOFT concern
    (`stacking_acceptance_unconfirmed`) — the funder hasn't published
    an opt-in, but absence of opt-in is not a published refusal.
    Operator confirms manually before submitting.
  - accepts_stacking=True  + max_positions None  -> SOFT concern
    (`stacking_max_unspecified`) — funder accepts stacking but cap is
    fuzzy; verify with funder before submitting.
  - accepts_stacking=False + deal positions == 0 -> no concern
    (clean first-position deal; stacking never engaged).

Pricing guidance (R4.2)
-----------------------
``compute_estimated_terms`` reads ``typical_factor_low/high`` and
``typical_holdback_low/high`` and produces a per-funder
``EstimatedTerms`` (advance / factor / holdback / daily payment / APR).
The score tier drives a linear interpolation: A → low end of the range
(most favorable to the merchant), F → high end (most defensive). APR is
computed via ``compliance.apr.calculate_apr`` — the actuarial method
required by Reg Z App J / CA 10 CCR § 950. When the optimizer cannot
bracket a root the APR is left as ``None`` rather than silently
substituting 0% (matches the R0.4 APR error gate discipline).

Auto-decline / conditional requirements (R4.3)
-----------------------------------------------
``FunderRow.auto_decline_conditions`` and ``.conditional_requirements``
are populated by the LLM guideline extractor as free-text strings. We
do NOT try to parse natural language. Instead:

- Each ``auto_decline_conditions`` entry surfaces as a hard fail when
  its lower-case form contains an "absolute" trigger (``decline``,
  ``do not fund``, ``must not``, ``no exception``, ``absolute``), or as
  a soft concern otherwise. Erring towards review — a soft concern the
  operator can promote is cheaper than ignoring a hard "do not fund".
- Each ``conditional_requirements`` entry surfaces as a soft concern
  with the verbatim text so the operator can verify with the funder.

`FunderRow` lives in `aegis.funders.models` (Phase 3.5). It is re-exported
here so existing callers (`from aegis.scoring.match_funders import FunderRow`)
continue to work.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aegis.compliance.apr import APRCalculationError, calculate_apr
from aegis.funders.models import FunderRow, FunderTier
from aegis.scoring.models import (
    EstimatedTerms,
    FunderMatch,
    ScoreInput,
    ScoreResult,
    TierMatch,
)
from aegis.scoring.pricing import estimate_tier_pricing

# Tier → position along the funder's pricing range, 0.0 = best end (low
# factor / low holdback, merchant-favorable), 1.0 = worst end (high
# factor / high holdback, defensive). A-tier deals get the funder's
# advertised low end; E/F deals price at the top of the range. Linear
# interpolation between is intentional — funders publish a range, not
# a curve, so anything more elaborate would be overfitting.
_TIER_INTERPOLATION_POSITION: Final[dict[str, Decimal]] = {
    "A": Decimal("0.00"),
    "B": Decimal("0.25"),
    "C": Decimal("0.50"),
    "D": Decimal("0.75"),
    "E": Decimal("1.00"),  # not currently emitted by scoring (Literal
    # restricts to A/B/C/D/F) but included for forward-compat and
    # explicit-intent documentation.
    "F": Decimal("1.00"),
}

# Quantization for display values. Factor / holdback at 4 decimal places
# (precise enough for "1.2875" without false precision); money at 2dp;
# APR returned from calculate_apr is already 6dp-quantized.
_RATE_QUANT: Final[Decimal] = Decimal("0.0001")
_MONEY_QUANT: Final[Decimal] = Decimal("0.01")

# Auto-decline trigger keywords. Matched on lower-cased entry text. A
# single match elevates the entry to a hard fail; otherwise it lands as
# a soft concern. Bias is intentional: false-positive hard_fail forces
# operator review (cheap); false-negative would silently submit a deal
# the funder has explicitly refused (expensive).
_AUTO_DECLINE_ABSOLUTE_TRIGGERS: Final[tuple[str, ...]] = (
    "decline",
    "do not fund",
    "must not",
    "no exception",
    "absolute",
)

# R3.4 — states whose broker statutes prohibit advance-fee solicitation.
# Mirrors ``broker_advance_fees_prohibited=True`` in
# ``aegis.compliance.states`` (FL § 559.952, GA § 7-7-3). Per CLAUDE.md
# "Decision-boundary changes — shadow-first" this is annotation only —
# the per-funder soft_concern fires when a funder's
# ``charges_merchant_advance_fees=True`` is paired with a merchant in
# one of these states. No hard fail, no tier change. Deal-level
# ``ScoreInput.advance_fees_charged`` remains the explicit-override
# path through ``_state_disclosure_flag`` in score.py.
_ADVANCE_FEE_PROHIBITED_STATES: Final[frozenset[str]] = frozenset({"FL", "GA"})


def compute_estimated_terms(
    funder: FunderRow,
    score: ScoreResult,
    deal: ScoreInput,
) -> EstimatedTerms | None:
    """Compute per-funder pricing guidance for a deal.

    Returns ``None`` when:
      - the funder has not published a pricing envelope
        (``typical_factor_low/high`` and ``typical_holdback_low/high``
        all unset), or
      - the score tier is outside the interpolation table, or
      - ``score.estimated_payback_days`` is missing (no term → no daily
        payment → no APR).

    The interpolation is linear between the funder's published low and
    high ends, indexed by ``_TIER_INTERPOLATION_POSITION``. See module
    docstring for the design rationale.
    """
    # Tier mapping — emit None when the tier is unknown / unrepresented.
    if score.tier not in _TIER_INTERPOLATION_POSITION:
        return None
    position = _TIER_INTERPOLATION_POSITION[score.tier]

    # Pricing envelope must be fully specified to interpolate. A funder
    # with only a low bound (or only a high bound) is ambiguous — we'd
    # rather show nothing than fabricate the other end.
    if (
        funder.typical_factor_low is None
        or funder.typical_factor_high is None
        or funder.typical_holdback_low is None
        or funder.typical_holdback_high is None
    ):
        return None

    if score.estimated_payback_days is None or score.estimated_payback_days <= 0:
        return None

    factor = _interpolate(
        funder.typical_factor_low, funder.typical_factor_high, position
    ).quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)
    holdback = _interpolate(
        funder.typical_holdback_low, funder.typical_holdback_high, position
    ).quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)

    # Advance: start from the score's suggested max, clamp into the
    # funder's [min, max] window. Funders that publish only one bound
    # constrain only that side.
    advance = score.suggested_max_advance
    if funder.min_advance is not None and advance < funder.min_advance:
        advance = funder.min_advance
    if funder.max_advance is not None and advance > funder.max_advance:
        advance = funder.max_advance
    advance = advance.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)

    payback_days = score.estimated_payback_days
    total_payback = advance * factor
    daily_payment = (total_payback / Decimal(payback_days)).quantize(
        _MONEY_QUANT, rounding=ROUND_HALF_UP
    )

    apr = _estimate_apr(advance, daily_payment, payback_days)

    evidence = (
        f"tier={score.tier} → {(position * Decimal('100')).quantize(Decimal('1'))}% "
        f"along factor range {funder.typical_factor_low}-{funder.typical_factor_high} "
        f"→ {factor}; holdback {funder.typical_holdback_low}-"
        f"{funder.typical_holdback_high} → {holdback}"
    )

    return EstimatedTerms(
        estimated_advance=advance,
        estimated_factor=factor,
        estimated_holdback_pct=holdback,
        estimated_daily_payment=daily_payment,
        estimated_apr=apr,
        interpolation_evidence=evidence,
    )


def _interpolate(low: Decimal, high: Decimal, position: Decimal) -> Decimal:
    """Linear interpolation between low and high at fractional position.

    position=0 → low, position=1 → high. No clamping — callers pass a
    value from ``_TIER_INTERPOLATION_POSITION`` which is bounded by
    construction.
    """
    return low + (high - low) * position


def _estimate_apr(
    advance: Decimal,
    daily_payment: Decimal,
    payback_days: int,
) -> Decimal | None:
    """Synthesize a daily payment stream and run the actuarial APR.

    Returns ``None`` when ``calculate_apr`` cannot converge — better to
    render "APR: unavailable" than the 0.00% silent fallback the legacy
    code path used (see R0.4 / docs/AUDIT_2026_05_10.md H7). ``date``
    is anchored at an arbitrary disbursement_date because APR depends
    only on the offsets, not the calendar position.
    """
    from datetime import date, timedelta

    if daily_payment <= 0 or advance <= 0:
        return None

    disbursement = date(2026, 1, 1)
    payments = [
        (disbursement + timedelta(days=offset), daily_payment)
        for offset in range(1, payback_days + 1)
    ]
    try:
        return calculate_apr(advance, payments, disbursement)
    except APRCalculationError:
        # No silent zero: surface as None and let the dossier render
        # "APR: unavailable" so the operator sees the gap.
        return None


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

    # Stacking — see module docstring "Stacking semantics" for the four branches.
    has_stacking_policy = (
        funder.max_positions is not None
        or funder.accepts_stacking
        or deal.mca_positions >= 1  # raises a soft concern even with default policy
    )
    if has_stacking_policy:
        criteria_count += 1
        if funder.max_positions is not None and deal.mca_positions > funder.max_positions:
            # Branch 2: published constraint is binding.
            hard.append(
                f"exceeds_max_positions: {deal.mca_positions} > max {funder.max_positions}"
            )
        elif not funder.accepts_stacking and deal.mca_positions >= 1:
            # Branch 1: no opt-in published; ambiguous default. Operator confirms.
            soft.append(
                "stacking_acceptance_unconfirmed: "
                "funder has not confirmed stacking acceptance — manual confirmation needed"
            )
        elif funder.accepts_stacking and funder.max_positions is None:
            # Branch 3: opt-in but cap is fuzzy. Verify before submitting.
            soft.append(
                "stacking_max_unspecified: "
                "stacking accepted but no maximum specified — verify with funder"
            )
        # else: branch 4 — clean first-position deal, or stacked deal within
        # the funder's published cap. No concern emitted.

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

    # R4.3: auto_decline_conditions + conditional_requirements
    # ---------------------------------------------------------
    # These fields are LLM-extracted free text. We do not parse natural
    # language — we surface every non-empty entry, classifying based on
    # absolute-language keywords. See module docstring "Auto-decline /
    # conditional requirements" for the rationale.
    auto_decline_hard, auto_decline_soft = _evaluate_auto_decline(
        funder.auto_decline_conditions
    )
    if funder.auto_decline_conditions:
        criteria_count += 1
    hard.extend(auto_decline_hard)
    soft.extend(auto_decline_soft)

    conditional_soft = _evaluate_conditional_requirements(
        funder.conditional_requirements
    )
    if funder.conditional_requirements:
        criteria_count += 1
    soft.extend(conditional_soft)

    # R3.4 — per-funder advance-fee enforcement signal. Pairs a funder
    # that admits to charging merchant-side advance fees with a merchant
    # in a state whose broker statute prohibits the practice (FL / GA).
    # Shadow-mode soft_concern only; no hard fail (the operator decides
    # whether to submit). The deal-level explicit-override path stays in
    # ``score._state_disclosure_flag``; this branch is the live wiring
    # that fires per FunderMatch because ``build_score_input`` cannot
    # know which funder a deal will be submitted to.
    if (
        funder.charges_merchant_advance_fees
        and deal.state.upper() in _ADVANCE_FEE_PROHIBITED_STATES
    ):
        criteria_count += 1
        soft.append(
            "state_enforcement_concern:"
            "FL_GA_advance_fee_prohibition_for_this_funder"
        )

    if criteria_count == 0:
        return None

    qualifies = len(hard) == 0
    likelihood = _likelihood(qualifies, soft, score.tier)
    estimated_terms = compute_estimated_terms(funder, score, deal)
    # U28 — SHADOW MODE per-tier evaluation. Does NOT influence
    # match_score, soft_concerns, reasons, or any field downstream code
    # reads for decline routing. See `evaluate_tier_matches` and the
    # `TierMatch` docstring for the contract.
    tier_matches = evaluate_tier_matches(funder, score, deal)
    return FunderMatch(
        funder_id=funder.id,
        funder_name=funder.name,
        match_score=likelihood,
        reasons=[f"tier_{score.tier}"] if qualifies else [],
        soft_concerns=hard + soft,  # union — caller wants the full picture
        estimated_terms=estimated_terms,
        tier_matches=tier_matches,
    )


def evaluate_tier_matches(
    funder: FunderRow,
    score: ScoreResult,
    deal: ScoreInput,
) -> list[TierMatch]:
    """U28 — evaluate the merchant against each of a funder's tiers.

    SHADOW MODE. Per CLAUDE.md "Decision-boundary changes — shadow-first",
    this helper is annotation-only — callers MUST NOT use the result to
    alter ``FunderMatch.match_score``, ``soft_concerns``, ``reasons``,
    ``hard_decline_reasons``, or any field that downstream code reads
    for decline routing. The operator validates against the corpus +
    live shadow rows; a later code change flips tier-aware matching to
    live.

    The check set mirrors the funder-level matcher's hard gates that
    map cleanly to per-tier policy:

      * ``min_credit_score`` ≤ ``deal.credit_score`` (missing credit
        score → ``credit_score_unknown`` reason; conservatively fails
        the tier so an explicit FICO requirement isn't bypassed by
        absence of data).
      * ``min_months_in_business`` ≤ ``deal.time_in_business_months``
        (same missing-data treatment).
      * ``min_monthly_revenue`` ≤ ``deal.monthly_revenue``.
      * ``max_positions`` ≥ ``deal.mca_positions``.

    Per-tier economics are sourced from the tier's own fields, NOT the
    funder-level ``typical_*`` envelope: ``buy_rate_low/high``,
    ``max_holdback``, and ``max_advance`` (the latter clamped against
    ``score.suggested_max_advance``).
    """
    if not funder.tiers:
        return []

    out: list[TierMatch] = []
    for tier in funder.tiers:
        reasons = _evaluate_tier_criteria(tier, deal)
        clamped_advance = _clamp_advance_to_tier(
            score.suggested_max_advance, tier.max_advance
        )
        # U37 — pricing guidance per tier. Computed against the clamped
        # advance so payback_total reflects the ceiling the tier actually
        # writes (Elite caps at $250k even when the score suggests more).
        # Falls back to ``score.suggested_max_advance`` when the tier
        # publishes no ``max_advance`` (clamp returns None in that case).
        pricing_advance = clamped_advance if clamped_advance is not None else (
            score.suggested_max_advance
            if score.suggested_max_advance > 0
            else None
        )
        pricing = estimate_tier_pricing(tier, pricing_advance)
        out.append(
            TierMatch(
                tier_name=tier.name,
                qualifies=len(reasons) == 0,
                disqualifying_reasons=reasons,
                estimated_factor_low=tier.buy_rate_low,
                estimated_factor_high=tier.buy_rate_high,
                estimated_holdback=tier.max_holdback,
                estimated_advance=clamped_advance,
                estimated_payback_total=pricing.payback_total,
                estimated_daily_payment=pricing.daily_payment_estimate,
            )
        )
    return out


def _evaluate_tier_criteria(tier: FunderTier, deal: ScoreInput) -> list[str]:
    """Collect per-axis failure reasons for a single tier.

    Missing merchant data on an axis the tier constrains is conservative-
    fail with an explicit ``*_unknown`` reason. Rationale: a tier that
    says "min 700 FICO" is a published policy; reporting "qualifies=True"
    just because credit_score is None hides the very gap the operator
    wants the matrix to surface.
    """
    reasons: list[str] = []

    if tier.min_credit_score is not None:
        if deal.credit_score is None:
            reasons.append("credit_score_unknown")
        elif deal.credit_score < tier.min_credit_score:
            reasons.append(
                f"credit {deal.credit_score} < min {tier.min_credit_score}"
            )

    if tier.min_months_in_business is not None:
        if deal.time_in_business_months is None:
            reasons.append("time_in_business_unknown")
        elif deal.time_in_business_months < tier.min_months_in_business:
            reasons.append(
                f"tib {deal.time_in_business_months}mo < "
                f"min {tier.min_months_in_business}mo"
            )

    if (
        tier.min_monthly_revenue is not None
        and deal.monthly_revenue < tier.min_monthly_revenue
    ):
        reasons.append(
            f"revenue ${deal.monthly_revenue} < min ${tier.min_monthly_revenue}"
        )

    if (
        tier.max_positions is not None
        and deal.mca_positions > tier.max_positions
    ):
        reasons.append(
            f"positions {deal.mca_positions} > max {tier.max_positions}"
        )

    return reasons


def _clamp_advance_to_tier(
    suggested: Decimal, tier_max: Decimal | None
) -> Decimal | None:
    """Clamp the score's suggested advance down to the tier's ceiling.

    Returns ``None`` when the tier did not publish a ``max_advance`` —
    no ceiling means "this tier isn't the ceiling-publisher", and
    fabricating one would be a tier-aware false precision.
    """
    if tier_max is None:
        return None
    clamped = suggested if suggested <= tier_max else tier_max
    return clamped.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _evaluate_auto_decline(
    entries: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Classify each auto_decline entry into (hard, soft).

    Entries whose lower-cased text contains any
    ``_AUTO_DECLINE_ABSOLUTE_TRIGGERS`` keyword become hard fails; the
    rest become soft concerns. Both branches preserve the funder's
    verbatim entry text so the operator can read the original language
    in the dossier.
    """
    hard: list[str] = []
    soft: list[str] = []
    for raw in entries:
        text = raw.strip()
        if not text:
            continue
        lowered = text.lower()
        if any(trigger in lowered for trigger in _AUTO_DECLINE_ABSOLUTE_TRIGGERS):
            hard.append(f"auto_decline: {text}")
        else:
            soft.append(f"auto_decline_review: {text} — review with funder")
    return hard, soft


def _evaluate_conditional_requirements(entries: tuple[str, ...]) -> list[str]:
    """Surface every non-empty conditional requirement as a soft concern."""
    out: list[str] = []
    for raw in entries:
        text = raw.strip()
        if not text:
            continue
        out.append(f"conditional: {text} — verify with funder")
    return out


def _likelihood(qualifies: bool, soft: list[str], tier: str) -> int:
    if not qualifies:
        return 0
    base = {"A": 90, "B": 75, "C": 60, "D": 40, "F": 0}[tier]
    return max(0, base - 10 * len(soft))


__all__ = [
    "FunderRow",
    "compute_estimated_terms",
    "evaluate_tier_matches",
    "match_funder",
]
# FunderRow is re-exported from aegis.funders.models — see module docstring.
