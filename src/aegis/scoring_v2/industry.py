"""Industry risk tiering — Close ``Industry`` choice -> AEGIS tier.

Reads the operator's Close-side ``Industry`` selection (already
persisted on the merchant row via the inbound webhook) and maps it
into one of five tiers that feed Track B's band adjustment:

* ``standard``           — recession-resistant, stable cash flow
                           (e.g. healthcare practices). No band
                           adjustment. Optional positive signal in
                           Track B's reasons list.
* ``moderate``           — no strong signal either way. Default for
                           anything not explicitly classified — also
                           the safe fallback for unknown strings. No
                           band adjustment.
* ``elevated``           — above-baseline risk (high-ticket retail,
                           regulated convenience, asset-intensive
                           manufacturing). Bumps Track B band ONE
                           step up.
* ``high_volatility``    — seasonal / cyclical / margin-thin (food
                           service, hospitality, construction,
                           fitness, trucking). Bumps Track B band
                           TWO steps up.
* ``hard_decline_class`` — categorically excluded industries
                           (cannabis, adult, firearms, MLM, etc.).
                           Forces Track B band to ``high`` regardless
                           of cash flow. SHADOW ONLY in v1 — no live
                           decline path even when this fires; the
                           dossier flags the merchant, the operator
                           judges. None of the current Close
                           ``Industry`` choices map here yet; the tier
                           exists for when those categories get added
                           to the Close dropdown.

Lookup is case-insensitive and hyphen-normalized: the Close Lead-side
``Industry`` field uses em-dash (``"Construction — General Contractor"``),
while the Opportunity-side ``Industry`` field uses regular hyphen
(``"Construction - General Contractor"``). Lead-side em-dash form is
the canonical key in :data:`INDUSTRY_RISK_TIERS`. Inputs in either
form normalize to the canonical key before lookup so callers don't
have to remember which Close field they're sourcing from.

Mapping rationale — for each non-moderate entry the
:data:`INDUSTRY_TIER_REASONS` table carries a one-line underwriter-
voice explanation. Surfaces on the dossier chip + the Track B
reasons list, and lets a future operator audit reproduce the
"why this tier?" without re-reading the source.

DECISION-BOUNDARY POSTURE — SHADOW ONLY
---------------------------------------
This module produces an informational tier. ``hard_decline_class``
forces Track B band to ``high`` (informational), but Track B itself
is shadow-only against the live decline path — the legacy
``fraud_score`` rule and the existing ``MCA_POSITIONS_HARD_DECLINE``
/ ``MAX_DEBT_TO_REVENUE`` gates govern declines. A future flip to
make ``hard_decline_class`` an actual auto-decline is a config /
env-var change, not a code deploy. See CLAUDE.md "Decision-boundary
changes — shadow-first".
"""

from __future__ import annotations

from typing import Final, Literal

IndustryTier = Literal[
    "standard",
    "moderate",
    "elevated",
    "high_volatility",
    "hard_decline_class",
]

UNKNOWN_INDUSTRY_TIER: Final[IndustryTier] = "moderate"
"""Safe default for unknown / missing industry strings. Per CLAUDE.md
op-principle #4 (never fill placeholder values from training data),
the default is conservative rather than permissive — an industry the
operator hasn't classified gets treated as ``moderate``, not
``standard``. The operator can promote a real value later; a wrong
``standard`` default would silently flatter unfamiliar industries."""


# Canonical lookup: Close Lead-side ``Industry`` em-dash strings ->
# tier. Source: ``find_lead_custom_fields`` MCP query 2026-06-15,
# field ``Industry`` (cf_Wls6nOfOp8CE8VNp4KkJxfZTSYlkqByKHkRwa0VQelr).
# All 25 enumerated choices appear here; ``industry_risk_tier``
# returns :data:`UNKNOWN_INDUSTRY_TIER` for anything else (including
# the Close ``-None-`` sentinel and operator-typed strings that
# aren't on the dropdown).
INDUSTRY_RISK_TIERS: Final[dict[str, IndustryTier]] = {
    # standard — recession-resistant, stable, low default risk
    "Healthcare — Dental": "standard",
    "Healthcare — Medical Practice": "standard",
    "Healthcare — Veterinary": "standard",
    # moderate — explicit (default also lands here)
    "Cleaning Services": "moderate",
    "Consulting": "moderate",
    "Legal Services": "moderate",
    "Other (Approved)": "moderate",
    "Professional Services": "moderate",
    "Real Estate Services": "moderate",
    "Technology": "moderate",
    "Wholesale / Distribution": "moderate",
    # elevated — above-baseline (asset-intensive, regulated,
    # high-ticket discretionary, single-source-of-revenue)
    "Auto Repair / Service": "elevated",
    "Beauty / Salon / Spa": "elevated",
    "Car Dealership": "elevated",
    "Convenience / Liquor / Gas": "elevated",
    "Convenience/Liquor/Gas": "elevated",  # operator-typed
    "Manufacturing": "elevated",
    "Retail — General": "elevated",
    "Retail — Specialty": "elevated",
    # high_volatility — seasonal / cyclical / margin-thin (the
    # NAICS-prefix-driven seasonality list in
    # ``aegis.scoring.score._SEASONAL_NAICS_PREFIXES`` overlaps with
    # this set: 7225 restaurants, 7223 catering, landscaping,
    # logging)
    "Construction — General Contractor": "high_volatility",
    "Construction — Specialty Trades": "high_volatility",
    "Fitness / Gym": "high_volatility",
    "Hospitality / Hotel": "high_volatility",
    "Restaurant / Food Service": "high_volatility",
    "Trucking / Logistics": "high_volatility",
    # hard_decline_class — none in current Lead choices. Mechanism
    # exists for when Close adds cannabis / adult / firearms / MLM
    # / debt-relief / MSB entries to the dropdown. Add here as the
    # operator confirms each.
}


INDUSTRY_TIER_REASONS: Final[dict[IndustryTier, str]] = {
    "standard": ("stable recurring revenue, low default risk in the MCA portfolio"),
    "moderate": ("no strong industry signal either way"),
    "elevated": ("above-baseline cyclicality or asset/regulation exposure"),
    "high_volatility": (
        "seasonal or margin-thin operating model; revenue cliffs are the default failure mode"
    ),
    "hard_decline_class": (
        "categorically excluded industry; no funder partner in the "
        "AEGIS network will price the deal"
    ),
}


def industry_risk_tier(industry: str | None) -> IndustryTier:
    """Map a Close Industry string to its risk tier.

    Returns :data:`UNKNOWN_INDUSTRY_TIER` (``"moderate"``) for any of:

    * ``None`` (merchant has no industry on file yet).
    * Empty / whitespace-only string.
    * Close's ``"-None-"`` sentinel.
    * Strings not present in :data:`INDUSTRY_RISK_TIERS` after
      normalization.

    Normalization rules:

    1. Strip surrounding whitespace.
    2. Replace regular ASCII hyphens with em-dashes so an
       Opportunity-side ``"Construction - General Contractor"`` resolves
       to the canonical Lead-side key
       ``"Construction — General Contractor"``. The dash replacement
       is restricted to ``" - "`` (space-hyphen-space) so token-internal
       hyphens (e.g. a future ``"X-Y Services"``) are preserved.

    Matching is case-sensitive by design — Close enforces fixed
    casing on dropdown choices, and a lenient match would mask real
    drift (an operator-typed ``"restaurant / food service"`` would
    map to the same tier as the canonical entry, hiding the fact
    that the operator didn't pick the dropdown value).
    """
    if industry is None:
        return UNKNOWN_INDUSTRY_TIER
    stripped = industry.strip()
    if not stripped or stripped == "-None-":
        return UNKNOWN_INDUSTRY_TIER
    canonical = stripped.replace(" - ", " — ")
    return INDUSTRY_RISK_TIERS.get(canonical, UNKNOWN_INDUSTRY_TIER)


def industry_tier_reason(tier: IndustryTier) -> str:
    """One-line underwriter-voice explanation for the dossier chip
    + the Track B FactorReason. Looked up from
    :data:`INDUSTRY_TIER_REASONS` — kept as a function so call sites
    don't import the dict directly (the table may grow per-industry
    overrides later)."""
    return INDUSTRY_TIER_REASONS[tier]


__all__ = [
    "INDUSTRY_RISK_TIERS",
    "INDUSTRY_TIER_REASONS",
    "UNKNOWN_INDUSTRY_TIER",
    "IndustryTier",
    "industry_risk_tier",
    "industry_tier_reason",
]
