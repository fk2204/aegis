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
from aegis.merchants.models import MerchantRow
from aegis.product_types import coerce_product_type
from aegis.scoring.models import (
    EstimatedTerms,
    FunderMatch,
    ScoreInput,
    ScoreResult,
    TierMatch,
)
from aegis.scoring.pricing import estimate_tier_pricing
from aegis.scoring_v2.offer import OfferRecommendation
from aegis.scoring_v2.stips import evaluate_stips

# Map free-form ``deal_types_accepted`` tokens (operator-curated, see
# `funders.repository.py`) to the canonical ``ProductType`` literal.
# Funders predate the Phase A product-type expansion, so the existing
# token vocabulary is preserved verbatim; a funder qualifies for a
# given product iff ANY of its tokens maps to that product.
#
# Multiple tokens can map to the same product (``term_loan`` and ``sba``
# both → business_loan). ``mca`` is treated as ``revenue_based`` — the
# product type Commera shipped originally.
_DEAL_TYPE_TO_PRODUCT: Final[dict[str, str]] = {
    "mca": "revenue_based",
    "revenue_based": "revenue_based",
    "rbf": "revenue_based",
    "term_loan": "business_loan",
    "loan": "business_loan",
    "sba": "business_loan",
    "business_line_of_credit": "line_of_credit",
    "loc": "line_of_credit",
    "line_of_credit": "line_of_credit",
    "equipment_financing": "equipment",
    "equipment": "equipment",
    "invoice_factoring": "receivables",
    "factoring": "receivables",
    "receivables": "receivables",
    "real_estate": "asset_based",
    "asset_based": "asset_based",
    "abl": "asset_based",
}


def _supports_product(
    deal_types_accepted: tuple[str, ...],
    product_type: str,
) -> bool:
    """Return True iff any of the funder's ``deal_types_accepted`` tokens
    maps to ``product_type`` via ``_DEAL_TYPE_TO_PRODUCT``.

    Unknown tokens contribute nothing — they don't accidentally grant
    eligibility for a product they don't name. Empty input returns
    False, but the caller is expected to short-circuit ``if
    funder.deal_types_accepted:`` before calling this helper, so the
    empty case is the "no constraint" default handled upstream.
    """
    for token in deal_types_accepted:
        if _DEAL_TYPE_TO_PRODUCT.get(token.lower().strip()) == product_type:
            return True
    return False


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
    offer: OfferRecommendation | None = None,
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

    Advance-seed precedence (2026-06-24 offer-sizing wire-through):
    when ``offer`` is not None, seed the funder-window clamp from
    ``offer.recommended_amount`` instead of ``score.suggested_max_advance``.
    Offer-derived sizing is capacity-aware + stack-overload-discounted
    while the legacy score field is a crude tier x revenue multiple.
    The funder ``[min_advance, max_advance]`` clamp logic is unchanged
    — only the value that gets clamped differs. Falls back to the
    legacy seed when ``offer`` is None so callers that don't compute
    an offer (the api/routes/deals.py ``POST /api/deals/{id}/score``
    legacy path keeps working) see no behaviour change.
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

    factor = _interpolate(funder.typical_factor_low, funder.typical_factor_high, position).quantize(
        _RATE_QUANT, rounding=ROUND_HALF_UP
    )
    holdback = _interpolate(
        funder.typical_holdback_low, funder.typical_holdback_high, position
    ).quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)

    # Advance: start from the offer's recommended amount (capacity-aware
    # + stack-overload-discounted) when supplied, otherwise the legacy
    # tier x revenue multiple. Clamp into the funder's [min, max] window;
    # funders that publish only one bound constrain only that side.
    advance = offer.recommended_amount if offer is not None else score.suggested_max_advance
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
    *,
    historical_approval_rate: Decimal | None = None,
    merchant: MerchantRow | None = None,
    offer: OfferRecommendation | None = None,
    bank_warning: str | None = None,
) -> FunderMatch | None:
    """Match a deal against a single funder. None if funder has no criteria configured.

    Tier-aware matching
    -------------------
    When ``funder.tiers`` is non-empty the per-tier matrix supersedes the
    funder-level revenue / credit / TIB / position minimums — those four
    axes are exactly what tiers carve up per risk level. We evaluate each
    tier in order (convention: best/cheapest first) and use the first
    qualifying tier's gates. If no tier qualifies, ``no_qualifying_tier``
    hard-fails the match. ADB stays funder-wide because ``FunderTier``
    has no ADB field; all the other funder-wide policies (exclusions,
    NSF, advance min/max, CoJ, broker compensation, advance-fee
    enforcement, auto-decline / conditional requirements) still apply.

    When ``funder.tiers`` is empty the existing funder-level minimums
    drive every gate — no behaviour change for funders that never
    published a tier matrix.

    Sprint 4 — ``historical_approval_rate`` boost
    ---------------------------------------------
    Optional caller-supplied prior-approval rate for similar deals
    (same merchant industry tier, same AEGIS score tier, last 90
    days, >= 5 sample). The caller — the route layer with access to
    the funder_note_submissions repo — pre-filters and computes the
    rate per (funder, industry_tier, score_tier) before invoking
    ``match_funder`` per funder. The matcher applies the boost AFTER
    the base score is computed and BEFORE the 0-100 cap:

      * rate >  0.60  ->  +5  match_score (track record positive)
      * rate <  0.20  ->  -10 match_score (track record poor)
      * 0.20 <= rate <= 0.60 OR rate is None  ->  no change

    The supplied rate also surfaces on ``FunderMatch.historical_approval_rate``
    so the dossier can render the track-record explicitly. Callers
    with no historical data pass ``None`` and the matcher behaves
    identically to pre-Sprint-4.
    """
    if not funder.active:
        return None

    # Product-type filter (no new column — uses existing
    # ``deal_types_accepted``). When the merchant has an EXPLICIT
    # ``product_type`` AND the funder has a non-empty
    # ``deal_types_accepted``, hard-exit if no token maps to the
    # merchant's product per ``_DEAL_TYPE_TO_PRODUCT``. Skip BEFORE
    # criteria evaluation so an Equipment funder never surfaces on a
    # revenue-based dossier even with a high score.
    #
    # IMPORTANT: legacy callers (merchant=None OR no product_type
    # attribute) get NO new filtering — the existing criteria-level
    # ``deal_types_accepted`` hard-fail (further down) is the
    # backwards-compatible path. Only merchants with an
    # operator-or-Close-set product_type opt into the early-exit
    # filter. This avoids regressing every existing call site that
    # passes ``deal.deal_type`` through the criteria path.
    explicit_product_type = getattr(merchant, "product_type", None)
    if explicit_product_type and funder.deal_types_accepted:
        product_type = coerce_product_type(explicit_product_type)
        if not _supports_product(funder.deal_types_accepted, product_type):
            return None

    hard: list[str] = []
    soft: list[str] = []
    criteria_count = 0
    has_tiers = bool(funder.tiers)

    # operator_status gates (migration 063). ``active`` is the no-op
    # default. The three non-default states fire BEFORE we evaluate
    # underwriting criteria so the reason surfaces at the top of the
    # match panel and the operator doesn't read a long hard-fail list
    # to discover that the funder isn't open for business this week.
    if funder.operator_status == "paused":
        criteria_count += 1
        hard.append("funder_paused: operator marked funder paused — no submissions")
    elif funder.operator_status == "first_position_only" and deal.mca_positions >= 1:
        criteria_count += 1
        hard.append(
            f"funder_first_position_only: deal has {deal.mca_positions} existing "
            f"MCA position(s); funder writes only first-position deals"
        )
    elif funder.operator_status == "selective":
        criteria_count += 1
        soft.append(
            "funder_selective_appetite: operator marked funder selective — "
            "confirm appetite before submitting"
        )

    # Revenue / credit / TIB are tier-driven when a matrix is published.
    # Skip the funder-level minimum to avoid double-gating against the
    # same axis. The per-tier evaluation below covers them.
    if not has_tiers:
        if funder.min_monthly_revenue is not None:
            criteria_count += 1
            if deal.monthly_revenue < funder.min_monthly_revenue:
                hard.append(f"revenue ${deal.monthly_revenue} < min ${funder.min_monthly_revenue}")

        if funder.min_credit_score is not None:
            criteria_count += 1
            if deal.credit_score is None:
                soft.append("credit_score_unknown")
            elif deal.credit_score < funder.min_credit_score:
                hard.append(f"credit {deal.credit_score} < min {funder.min_credit_score}")

        if funder.min_months_in_business is not None:
            criteria_count += 1
            if deal.time_in_business_months is None:
                soft.append("time_in_business_unknown")
            elif deal.time_in_business_months < funder.min_months_in_business:
                hard.append(
                    f"tib {deal.time_in_business_months}mo < min {funder.min_months_in_business}mo"
                )

    # ADB stays funder-wide — ``FunderTier`` carries no ADB field.
    if funder.min_avg_daily_balance is not None:
        criteria_count += 1
        if deal.avg_daily_balance < funder.min_avg_daily_balance:
            hard.append(f"adb ${deal.avg_daily_balance} < min ${funder.min_avg_daily_balance}")

    # Stacking — funder-wide semantics. When tiers are published, the
    # tier evaluation handles the ``max_positions`` hard cap per tier;
    # the funder-level ``accepts_stacking`` soft signals (the "operator
    # must manually confirm stacking" branches) still apply because
    # they're funder-wide policy, not tier-level.
    funder_max_positions_active = funder.max_positions is not None and not has_tiers
    has_stacking_policy = (
        funder_max_positions_active
        or funder.accepts_stacking
        or deal.mca_positions >= 1  # raises a soft concern even with default policy
    )
    if has_stacking_policy:
        criteria_count += 1
        if (
            funder_max_positions_active
            and funder.max_positions is not None
            and deal.mca_positions > funder.max_positions
        ):
            # Branch 2: published constraint is binding (only when no tier
            # matrix is in play; tiers handle ``max_positions`` per tier).
            hard.append(f"exceeds_max_positions: {deal.mca_positions} > max {funder.max_positions}")
        elif not funder.accepts_stacking and deal.mca_positions >= 1:
            # Branch 1: no opt-in published; ambiguous default. Operator confirms.
            soft.append(
                "stacking_acceptance_unconfirmed: "
                "funder has not confirmed stacking acceptance — manual confirmation needed"
            )
        elif funder.accepts_stacking and funder.max_positions is None and not has_tiers:
            # Branch 3: opt-in but cap is fuzzy. Verify before submitting.
            # Suppressed when a tier matrix is in play — the qualifying
            # tier's ``max_positions`` is the explicit cap.
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
            hard.append(f"requested ${deal.requested_amount} < min advance ${funder.min_advance}")

    if funder.max_advance is not None:
        criteria_count += 1
        if deal.requested_amount > funder.max_advance:
            hard.append(f"requested ${deal.requested_amount} > max advance ${funder.max_advance}")

    if funder.excluded_industries:
        criteria_count += 1
        matched_token = _excluded_industry_match(
            funder.excluded_industries,
            industry_choice=deal.industry_choice,
            industry_naics=deal.industry_naics,
        )
        if matched_token is not None:
            hard.append(f"industry_excluded: {matched_token}")

    if funder.excluded_states:
        criteria_count += 1
        if deal.state.upper() in {s.upper() for s in funder.excluded_states}:
            hard.append(f"state_excluded: {deal.state}")

    # Deal-type policy (migration 056). When the funder publishes
    # ``deal_types_accepted``, the deal's product must be on the
    # list. Empty tuple = "no constraint" (legacy behaviour). Matching
    # is case-insensitive on the canonical lowercase tokens the
    # extractor emits (``"mca"``, ``"term_loan"``, ``"loc"``, …).
    if funder.deal_types_accepted:
        criteria_count += 1
        if deal.deal_type.strip().lower() not in {
            t.strip().lower() for t in funder.deal_types_accepted
        }:
            hard.append(f"deal_type_not_accepted: {deal.deal_type}")

    # Funding velocity vs Close Urgency (migration 056). Soft concern
    # only — slow funders aren't disqualified, but the operator gets a
    # heads-up to set expectations when an ASAP merchant lands on a
    # funder that takes longer than 2 business days. Threshold is
    # intentionally generous: ``"ASAP (24-48 hours)"`` translates to
    # 1-2 business days; ``> 2`` is the first level that doesn't
    # mathematically fit.
    if (
        funder.funding_velocity_days is not None
        and deal.urgency is not None
        and deal.urgency.strip().lower().startswith("asap")
        and funder.funding_velocity_days > 2
    ):
        criteria_count += 1
        soft.append(
            "funding_velocity_mismatch: "
            f"asap_merchant_funder_takes_{funder.funding_velocity_days}_business_days"
        )

    # Preferred-state soft concern (migration 056). Distinct from
    # ``excluded_states`` which hard-fails. Funder published a soft
    # preference but will still write outside the list; operator
    # decides whether the discount in likelihood is worth it.
    if funder.preferred_states:
        criteria_count += 1
        if deal.state.upper() not in {s.upper() for s in funder.preferred_states}:
            soft.append(f"state_not_preferred: {deal.state}")

    # NY § 600.21(f) broker-compensation requirement is intentionally
    # NOT enforced here. Per ``.claude/rules/compliance.md`` SCOPE NOTE
    # (2026-05-25) Commera operates as a pure ISO broker; per-state
    # CFDL disclosure obligations (including the § 600.21(f) broker-
    # compensation letter) are funder concerns and must not gate
    # broker-side routing. The earlier wire-in (commit 6f595a4) hard-
    # failed every NY merchant against every funder lacking
    # ``aegis_compensation_disclosure_text`` — with 0/28 funders
    # carrying text in production this produced 100% no-match for NY
    # merchants, contradicting the scope rule that broker behavior
    # must not branch on state CFDL tiers.
    #
    # The disclosure letter pipeline (``record_broker_compensation_transmission``)
    # remains intact for callers that explicitly choose to render +
    # archive the letter (e.g. the submission-time disclosure path).
    # Matching does not pre-flight it.

    # R4.3: auto_decline_conditions + conditional_requirements
    # ---------------------------------------------------------
    # These fields are LLM-extracted free text. We do not parse natural
    # language — we surface every non-empty entry, classifying based on
    # absolute-language keywords. See module docstring "Auto-decline /
    # conditional requirements" for the rationale.
    auto_decline_hard, auto_decline_soft = _evaluate_auto_decline(funder.auto_decline_conditions)
    if funder.auto_decline_conditions:
        criteria_count += 1
    hard.extend(auto_decline_hard)
    soft.extend(auto_decline_soft)

    conditional_soft = _evaluate_conditional_requirements(funder.conditional_requirements)
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
        soft.append("state_enforcement_concern:FL_GA_advance_fee_prohibition_for_this_funder")

    # Tier-matrix qualification (live). When the funder publishes a
    # tier matrix, run it here so the qualifying-tier result feeds
    # ``reasons`` and ``match_score``. ``no_qualifying_tier`` hard-
    # fails the match — the funder won't write at any of its published
    # risk levels for this merchant.
    qualifying_tier: FunderTier | None = None
    qualifying_tier_position: int = -1
    if has_tiers:
        criteria_count += 1
        picked = _pick_qualifying_tier(funder.tiers, deal)
        if picked is None:
            hard.append("no_qualifying_tier")
        else:
            qualifying_tier, qualifying_tier_position = picked

    if criteria_count == 0:
        return None

    qualifies = len(hard) == 0
    reasons: list[str] = []
    if qualifies:
        if qualifying_tier is not None:
            reasons.append(f"qualifies_at_tier:{qualifying_tier.name}")
        reasons.append(f"tier_{score.tier}")

    if qualifying_tier is not None and qualifies:
        likelihood = _tier_match_score(qualifying_tier_position, soft)
    else:
        likelihood = _likelihood(qualifies, soft, score.tier)

    # Sprint 4 — historical-approval boost (applied AFTER base score,
    # BEFORE the 0-100 cap). See the docstring above for the band.
    likelihood = _apply_historical_boost(likelihood, historical_approval_rate)

    estimated_terms = compute_estimated_terms(funder, score, deal, offer=offer)
    # Per-tier shadow rows still surface on the dossier so the operator
    # sees the full matrix (which tiers qualified, which didn't, and why).
    # The qualifying tier above drives match_score; the full ``tier_matches``
    # below drives the operator-facing detail panel.
    tier_matches = evaluate_tier_matches(funder, score, deal, offer=offer)

    # Sprint 6 — structured stipulations. When the caller supplies the
    # merchant, evaluate the funder's ``conditional_requirements`` against
    # the merchant's on-file flags and surface (a) verbatim missing-stip
    # text on the match, (b) a soft concern per hard-missing stip. Legacy
    # callers (no merchant kwarg) get the identical pre-Sprint-6 behaviour:
    # ``missing_stips=[]`` and no additional soft concerns.
    missing_stips: list[str] = []
    stip_soft_concerns: list[str] = []
    if merchant is not None:
        stips_result = evaluate_stips(funder, merchant)
        for missing_item in stips_result.missing:
            missing_stips.append(missing_item.requirement_text)
            if missing_item.is_hard:
                stip_soft_concerns.append(f"missing stip: {missing_item.requirement_text}")

    # Web-presence reputation flags (migration 067). Each flag from the
    # most-recent ``scan_web_presence`` call surfaces as one soft
    # concern, prefixed so the dossier renderer can distinguish them
    # from underwriting concerns. Empty list = no flags = no concern.
    web_presence_soft_concerns: list[str] = []
    if merchant is not None and merchant.web_presence_flags:
        for flag in merchant.web_presence_flags:
            web_presence_soft_concerns.append(f"web presence: {flag}")

    # UCC + previous-default findings (migration 068). Each UCC filing
    # and each default indicator surfaces as one soft concern with the
    # ``UCC filing found:`` / ``Previous default indicator:`` prefix
    # the operator instructed.
    ucc_soft_concerns: list[str] = []
    if merchant is not None:
        for party in merchant.ucc_filings or []:
            ucc_soft_concerns.append(f"UCC filing found: {party}")
        for indicator in merchant.ucc_default_indicators or []:
            ucc_soft_concerns.append(f"Previous default indicator: {indicator}")

    # Fintech bank-of-record warning (parser-emitted, warning-only).
    # When the parser detected the merchant banks with a fintech /
    # neobank (Mercury, Brex, Novo, etc.), the caller passes the
    # pre-formatted warning string here and we attach it to EVERY
    # match so the operator sees the same caveat on every funder card.
    # See ``parser.fintech_banks`` for why this is warning-only.
    fintech_soft_concerns: list[str] = []
    if bank_warning is not None:
        fintech_soft_concerns.append(bank_warning)

    return FunderMatch(
        funder_id=funder.id,
        funder_name=funder.name,
        match_score=likelihood,
        reasons=reasons,
        # Union of hard fails + soft concerns + per-merchant stip soft
        # concerns + web-presence flags + UCC/default findings + the
        # parser-emitted fintech-bank warning — caller wants the full
        # picture.
        soft_concerns=(
            hard
            + soft
            + stip_soft_concerns
            + web_presence_soft_concerns
            + ucc_soft_concerns
            + fintech_soft_concerns
        ),
        estimated_terms=estimated_terms,
        tier_matches=tier_matches,
        historical_approval_rate=historical_approval_rate,
        missing_stips=missing_stips,
    )


def _pick_qualifying_tier(
    tiers: tuple[FunderTier, ...], deal: ScoreInput
) -> tuple[FunderTier, int] | None:
    """Walk ``tiers`` in order; return the first tier the deal qualifies
    for and its 0-based position. ``None`` when no tier qualifies.

    Convention (per ``FunderTier`` docstring) is most-permissive-for-
    the-merchant first — i.e. Elite (lowest buy rate, tightest
    criteria) before B before C. The first qualifying tier is therefore
    the BEST tier the merchant can land — and the highest match_score
    they should be eligible for.
    """
    for idx, tier in enumerate(tiers):
        if not _evaluate_tier_criteria(tier, deal):
            return tier, idx
    return None


_HISTORICAL_BOOST_HIGH_THRESHOLD: Final[Decimal] = Decimal("0.60")
"""Boost ``match_score`` by ``+5`` when the funder's similar-deal
approval rate is strictly greater than this. ``> 0.60`` per the
operator's spec — 60% exactly does NOT boost (the band is open)."""

_HISTORICAL_BOOST_LOW_THRESHOLD: Final[Decimal] = Decimal("0.20")
"""Penalise ``match_score`` by ``-10`` when the rate is strictly less
than this. ``< 0.20`` per the operator's spec — 20% exactly does NOT
penalise."""

_HISTORICAL_BOOST_AMOUNT: Final[int] = 5
_HISTORICAL_PENALTY_AMOUNT: Final[int] = -10
_MATCH_SCORE_MAX: Final[int] = 100
_MATCH_SCORE_MIN: Final[int] = 0


def _apply_historical_boost(
    base_score: int,
    historical_approval_rate: Decimal | None,
) -> int:
    """Project a base ``match_score`` + an optional historical rate
    onto the boost-adjusted, capped score.

    Pure function. ``None`` rate (no sample, insufficient sample, or
    caller didn't supply) returns ``base_score`` unchanged. Result
    clamped to ``[0, 100]`` so the +5 doesn't overflow past the
    ``Field(le=100)`` constraint on FunderMatch.match_score.
    """
    if historical_approval_rate is None:
        return base_score
    if historical_approval_rate > _HISTORICAL_BOOST_HIGH_THRESHOLD:
        adjusted = base_score + _HISTORICAL_BOOST_AMOUNT
    elif historical_approval_rate < _HISTORICAL_BOOST_LOW_THRESHOLD:
        adjusted = base_score + _HISTORICAL_PENALTY_AMOUNT
    else:
        adjusted = base_score
    return max(_MATCH_SCORE_MIN, min(_MATCH_SCORE_MAX, adjusted))


def _tier_match_score(position: int, soft: list[str]) -> int:
    """Tier position -> base match_score, then -10 per soft concern.

    * Position 0 (best/cheapest tier qualifies) -> base 90
    * Position 1                                -> base 75
    * Position 2                                -> base 60
    * Position 3                                -> base 45
    * Position 4+                               -> base 30 (floor)

    Same ``-10 * soft`` penalty as the funder-level ``_likelihood``
    path so the dossier comparison between tier-funders and funder-
    level funders stays calibrated.
    """
    base = max(30, 90 - position * 15)
    return max(0, base - 10 * len(soft))


def evaluate_tier_matches(
    funder: FunderRow,
    score: ScoreResult,
    deal: ScoreInput,
    offer: OfferRecommendation | None = None,
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

    # 2026-06-24 offer-sizing wire-through: when an OfferRecommendation
    # is supplied, seed the tier clamp from ``offer.recommended_amount``
    # (capacity-aware + stack-overload-discounted). Falls back to the
    # legacy ``score.suggested_max_advance`` when offer is None, so
    # pre-wire-through callers see no behaviour change.
    base_seed = offer.recommended_amount if offer is not None else score.suggested_max_advance

    out: list[TierMatch] = []
    for tier in funder.tiers:
        reasons = _evaluate_tier_criteria(tier, deal)
        clamped_advance = _clamp_advance_to_tier(base_seed, tier.max_advance)
        # U37 — pricing guidance per tier. Computed against the clamped
        # advance so payback_total reflects the ceiling the tier actually
        # writes (Elite caps at $250k even when the score suggests more).
        # Falls back to ``base_seed`` when the tier publishes no
        # ``max_advance`` (clamp returns None in that case).
        pricing_advance = (
            clamped_advance
            if clamped_advance is not None
            else (base_seed if base_seed > 0 else None)
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
            reasons.append(f"credit {deal.credit_score} < min {tier.min_credit_score}")

    if tier.min_months_in_business is not None:
        if deal.time_in_business_months is None:
            reasons.append("time_in_business_unknown")
        elif deal.time_in_business_months < tier.min_months_in_business:
            reasons.append(
                f"tib {deal.time_in_business_months}mo < min {tier.min_months_in_business}mo"
            )

    if tier.min_monthly_revenue is not None and deal.monthly_revenue < tier.min_monthly_revenue:
        reasons.append(f"revenue ${deal.monthly_revenue} < min ${tier.min_monthly_revenue}")

    if tier.max_positions is not None and deal.mca_positions > tier.max_positions:
        reasons.append(f"positions {deal.mca_positions} > max {tier.max_positions}")

    return reasons


def _clamp_advance_to_tier(suggested: Decimal, tier_max: Decimal | None) -> Decimal | None:
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


def _excluded_industry_match(
    excluded_tokens: tuple[str, ...],
    *,
    industry_choice: str | None,
    industry_naics: str | None,
) -> str | None:
    """Return the first excluded token that matches the deal's industry,
    or ``None`` when no exclusion fires.

    Two strategies, tried in order:

    1. **Word-set match against ``industry_choice``** — the Close Lead-
       side ``Industry`` string the merchant carries (em-dash form,
       e.g. ``"Restaurant / Food Service"``). The token's tokenised
       words (after lowercasing + non-alphanumeric → space → split)
       must be a subset of the choice's tokenised words. So
       ``"restaurant"`` -> ``{"restaurant"}`` is a subset of
       ``{"restaurant", "food", "service"}`` and the exclusion fires;
       ``"trucking"`` -> ``{"trucking"}`` is NOT a subset and skips.
       Hyphenated tokens like ``"adult-entertainment"`` tokenise to
       ``{"adult", "entertainment"}`` which matches against
       ``"Adult / Entertainment"`` even though the punctuation differs.

    2. **Fallback against NAICS-derived industry name** — when the
       merchant has no ``industry_choice`` on file (legacy pre-
       migration 055 rows or operator-typed NAICS without a Close
       Industry pick), reverse-look the NAICS code through
       ``CLOSE_INDUSTRY_TO_NAICS`` to derive the canonical choice
       string, then apply the same word-set test. Lossy by design
       (multiple choices can share a NAICS), but every entry in
       ``CLOSE_INDUSTRY_TO_NAICS`` is documented operator-curated;
       the reverse lookup is deterministic for those rows.

    Returns the matched token verbatim (lowercased / hyphenated form
    the extractor produced) so the operator sees what fired in the
    ``industry_excluded:`` reason.
    """
    candidates: list[str] = []
    if industry_choice:
        candidates.append(industry_choice)
    if industry_naics:
        derived = _industry_name_from_naics(industry_naics)
        if derived is not None:
            candidates.append(derived)

    if not candidates:
        return None

    candidate_word_sets = [_industry_words(c) for c in candidates]
    for raw_token in excluded_tokens:
        token_words = _industry_words(raw_token)
        if not token_words:
            continue
        for words in candidate_word_sets:
            if token_words.issubset(words):
                return raw_token
    return None


def _industry_words(text: str) -> frozenset[str]:
    """Lowercase, replace non-alphanumeric chars with spaces, split
    into a word set. Used by ``_excluded_industry_match`` to compare
    funder exclusion tokens against the merchant's industry string
    without caring about hyphens vs spaces vs slashes."""
    lowered = text.lower()
    cleaned = "".join(c if c.isalnum() else " " for c in lowered)
    return frozenset(w for w in cleaned.split() if w)


def _industry_name_from_naics(naics: str) -> str | None:
    """Reverse lookup ``CLOSE_INDUSTRY_TO_NAICS`` — given a 6-digit
    NAICS code, return the first matching Close Industry choice
    string. ``None`` when the code isn't in the table.

    Imported lazily so the scoring layer doesn't pull in
    ``aegis.close`` at module-import time (the close package loads
    integration plumbing this gate doesn't need)."""
    from aegis.close.field_map import CLOSE_INDUSTRY_TO_NAICS

    for choice, code in CLOSE_INDUSTRY_TO_NAICS.items():
        if code == naics:
            return choice
    return None


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
