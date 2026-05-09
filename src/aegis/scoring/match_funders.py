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

Unused fields
-------------
`typical_factor_low/high` and `typical_holdback_low/high` on FunderRow
are extracted from guideline PDFs and stored, but match scoring does
not use them yet. They are reserved for the v1.1 ranking enhancement
(reordering equally-qualified funders by pricing fit to the deal).

`FunderRow` lives in `aegis.funders.models` (Phase 3.5). It is re-exported
here so existing callers (`from aegis.scoring.match_funders import FunderRow`)
continue to work.
"""

from __future__ import annotations

from aegis.compliance.states import STATES, Tier1Regulation
from aegis.funders.models import FunderRow
from aegis.logger import get_logger
from aegis.scoring.models import FunderMatch, ScoreInput, ScoreResult

_log = get_logger(__name__)


def _coj_state_rule(state_code: str) -> tuple[str | None, str | None]:
    """Resolve the merchant's state to (coj_status, citation).

    Drives off the Tier 1 ``StateRegulation`` table so promoting a new
    state to Tier 1 with ``coj_allowed`` set automatically activates the
    matcher rule — no second source of truth here. States that aren't
    Tier 1 (or aren't served at all) return ``(None, None)`` and pass
    through.

    Returns
    -------
    (status, citation)
        status ∈ {"banned", "conditional", "allowed", None}.
        citation is the state's ``coj_citation`` when status is non-None.
    """
    reg = STATES.get(state_code)
    if not isinstance(reg, Tier1Regulation):
        return (None, None)
    return (reg.coj_allowed, reg.coj_citation)


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

    # State-level CoJ rule — driven off the Tier 1 STATES table:
    #   * "banned"      → hard fail (CA per Cal. Code Civ. Proc. § 1132).
    #     Reason `coj_invalid_in_state`; warning log
    #     `funder_requires_coj_blocked_by_state`. The CoJ clause would be
    #     unenforceable in this state and the deal cannot ship.
    #   * "conditional" → soft concern (NY per CPLR § 3218 — CoJs only
    #     enforceable against NY-resident merchants since 2019-08-30, see
    #     docs/compliance/02_new_york.md). Reason `coj_ny_resident_only`;
    #     info log of the same name. Operator confirms merchant residency
    #     before transmitting the funder agreement.
    #   * "allowed" / not Tier 1 → no concern.
    state_code = (deal.state or "").upper()
    if funder.requires_coj:
        coj_status, coj_citation = _coj_state_rule(state_code)
        if coj_status == "banned":
            criteria_count += 1
            hard.append(f"coj_invalid_in_state: {coj_citation}")
            _log.warning(
                "funder_requires_coj_blocked_by_state funder_id=%s funder_name=%s "
                "merchant_state=%s citation=%s",
                funder.id,
                funder.name,
                state_code,
                coj_citation,
            )
        elif coj_status == "conditional":
            criteria_count += 1
            soft.append(
                f"coj_ny_resident_only: {coj_citation} permits CoJ only "
                "against NY-resident merchants — verify principal place of "
                "business before transmitting funder agreement"
            )
            _log.info(
                "coj_ny_resident_only funder_id=%s funder_name=%s "
                "merchant_state=%s citation=%s",
                funder.id,
                funder.name,
                state_code,
                coj_citation,
            )

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
# FunderRow is re-exported from aegis.funders.models — see module docstring.
