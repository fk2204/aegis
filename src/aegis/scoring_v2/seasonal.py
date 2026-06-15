"""Seasonal-trough detector — suppresses false-positive ``revenue_declining_15pct+``.

The legacy ``_score_revenue_trend`` rule in :mod:`aegis.scoring.score`
flags any deal whose last 3-month deposits are down 15%+ from the
3-months-prior window. For non-seasonal businesses that is a real
signal; for industries with a known low season (restaurants in
January, construction in February, landscaping in winter, retail
post-holiday return wave) it is a routine seasonal trough — not a
declining-revenue risk.

This module is the dedicated detector: given an industry signal
(NAICS prefix and/or Close ``Industry`` choice) and the per-month
deposit breakdown, return ``True`` iff the observed 3-month dip
matches an industry-typical seasonal pattern.

Decision-boundary posture
-------------------------
The detector itself is pure and side-effect-free. The wire-in point
(``aegis.scoring.score._score_revenue_trend``) uses it to *suppress*
the existing -15 penalty when the trough is explainable and emit a
zero-weight ``seasonal_trough_expected`` annotation in its place. The
behavior change is operator-visible (the dossier line item swaps
from a -15 deduction to an annotation) but mathematically additive
in aggregate — the score increases by 15 for affected deals, the
score for non-seasonal businesses is unchanged.

Per CLAUDE.md "Decision-boundary changes — shadow-first" the
expectation is that this detector ships with corpus validation
demonstrating it does not silently approve declining-revenue deals
that happen to fall in a low-season month. Wire-in tests in
``tests/scoring/test_seasonal.py`` lock the integration shape.

Industry map sources
--------------------
NAICS prefixes and trough months are sourced from public BLS /
Census Retail Trade seasonal-adjustment factors. The starter set is
conservative — covers industries where the trough is well-documented
across the brokerage's customer mix and where a 25% dip below the
trailing-12-month average is the *expected* seasonal pattern rather
than an underwriting concern.

Anything not in the map returns ``False`` — we never assume a
business is seasonal without an explicit signal. Adding to the map
requires operator + corpus validation; never extend from training
data alone (CLAUDE.md op-principle #4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from aegis.scoring.models import MonthBreakdown

# Industry → expected trough months (1-12). Sources:
#   - Restaurants (7225): BLS food-services seasonal factors show a
#     pronounced Jan/Feb trough after the Dec holiday peak.
#   - Construction (23): Census Construction Spending seasonal factors
#     show Dec/Jan/Feb winter trough (northern markets cannot pour
#     concrete; permits drop).
#   - Retail General/Specialty (44/45 excl. 451 sporting/hobby):
#     Census Retail Trade seasonal factors show Jan/Feb post-holiday
#     return wave + slow Q1.
#   - Landscaping (56173): trivially seasonal — Dec/Jan/Feb winter
#     dormancy.
#   - Agriculture (11): conservative Dec-Mar window covering the
#     pre-spring-planting dip for the brokerage's typical row-crop /
#     grain customer mix.
#   - Hospitality / Hotel (7211): leisure-travel Jan/Feb lull. Ski
#     markets buck the pattern and are not auto-handled here.
_SEASONAL_NAICS_TROUGHS: Final[dict[str, frozenset[int]]] = {
    "11": frozenset({12, 1, 2, 3}),  # Agriculture
    "23": frozenset({12, 1, 2}),  # Construction (general + specialty)
    "44": frozenset({1, 2}),  # Retail — general
    "45": frozenset({1, 2}),  # Retail — specialty (excl. 451 below)
    "56173": frozenset({12, 1, 2}),  # Landscaping
    "7211": frozenset({1, 2}),  # Hospitality / Hotel
    "7225": frozenset({1, 2}),  # Restaurants
}

# NAICS prefixes that look retail by 2-digit code but are NOT in the
# trough window — sporting goods + hobby + book + music stores (451)
# actually peak in December (holiday gift) and do not have a clean
# Jan/Feb trough. Exclude them so the 44/45 generic mapping does not
# silently capture them.
_NAICS_PREFIX_EXCLUSIONS: Final[frozenset[str]] = frozenset({"451"})

# Close Lead-side ``Industry`` choice → trough months. Mirrors the
# NAICS map for businesses where the Close Industry field is more
# reliable than the NAICS code (the operator picks Industry; NAICS
# is sometimes derived). String keys match
# ``INDUSTRY_RISK_TIERS`` em-dash form (Close canonical).
_SEASONAL_INDUSTRY_TROUGHS: Final[dict[str, frozenset[int]]] = {
    "Restaurant / Food Service": frozenset({1, 2}),
    "Construction — General Contractor": frozenset({12, 1, 2}),
    "Construction — Specialty Trades": frozenset({12, 1, 2}),
    "Hospitality / Hotel": frozenset({1, 2}),
    "Retail — General": frozenset({1, 2}),
    "Retail — Specialty": frozenset({1, 2}),
}

# Dip threshold. Last-3-month average must be > 25% below trailing-12-month
# average to count as a seasonal-trough explanation. A shallower dip is
# not the seasonal story — fall through and let the existing penalty
# fire if the merchant-vs-prior-window comparison still trips it.
_SEASONAL_DIP_THRESHOLD: Final[Decimal] = Decimal("0.25")

# Minimum window length the trailing-12-month average needs to be
# meaningful. Below this we fall back to "whatever's available" and
# the comparison degrades gracefully — but never compute the trailing
# average over fewer than 3 months (it would collapse into the same
# window as the last3 numerator).
_MIN_BASELINE_MONTHS: Final[int] = 3


def _industry_trough_months(
    *,
    industry_naics: str | None,
    industry_choice: str | None,
) -> frozenset[int] | None:
    """Return the trough-month set for this industry, or ``None`` when
    the industry has no recorded seasonal pattern.

    Resolution order:

    1. Close ``Industry`` choice (operator-curated, canonical).
    2. NAICS prefix match, longest-prefix-first so ``56173`` (landscaping)
       wins over the implicit ``5`` parent.
    3. ``None`` if neither matches.

    NAICS exclusions (``451`` sporting/hobby) short-circuit the 44/45
    retail mapping — those stores peak rather than trough in Jan/Feb.
    """
    if industry_choice is not None:
        normalized = industry_choice.strip().replace(" - ", " — ")
        if normalized in _SEASONAL_INDUSTRY_TROUGHS:
            return _SEASONAL_INDUSTRY_TROUGHS[normalized]
    if industry_naics is None:
        return None
    naics = industry_naics.strip()
    if not naics:
        return None
    # Exclusions first — sporting/hobby retail (451) is in the 44/45
    # parent space but peaks in December.
    for excluded in _NAICS_PREFIX_EXCLUSIONS:
        if naics.startswith(excluded):
            return None
    # Longest prefix wins so 56173 beats a hypothetical 5 parent.
    for prefix in sorted(_SEASONAL_NAICS_TROUGHS, key=len, reverse=True):
        if naics.startswith(prefix):
            return _SEASONAL_NAICS_TROUGHS[prefix]
    return None


def _avg_deposits(buckets: list[MonthBreakdown]) -> Decimal:
    """Mean of ``deposits`` across the given buckets. ``Decimal("0")`` for
    empty input — the caller guards on that case before using the value."""
    if not buckets:
        return Decimal("0")
    total = sum((b.deposits for b in buckets), start=Decimal("0"))
    return total / Decimal(len(buckets))


def is_seasonal_trough(
    *,
    industry_naics: str | None,
    industry_choice: str | None,
    month_buckets: list[MonthBreakdown],
    now: datetime | None = None,
) -> bool:
    """True iff a 3-month dip is explainable by the merchant's industry's
    expected low season — not a real revenue decline.

    Returns True iff ALL of the following hold:

    1. The industry has a known seasonal pattern (Close ``Industry``
       choice or NAICS prefix match against the curated map above).
    2. Trailing 3-month average deposits is > 25% below the trailing
       12-month average deposits. When fewer than 12 months of data is
       available, the baseline collapses to whatever months precede
       the trailing-3 window. Below 3 baseline months we return False —
       the comparison is too noisy to defend.
    3. The current month (``now.month``, default UTC today) falls
       within the industry's expected trough window.

    Returns False otherwise — including when the industry isn't
    seasonal, when the dip is too shallow to be the seasonal
    explanation, or when the timing doesn't fit the industry's trough
    months.

    Pure function. Decimal-only money math. Safe to call from
    ``_score_revenue_trend``.
    """
    troughs = _industry_trough_months(
        industry_naics=industry_naics, industry_choice=industry_choice
    )
    if troughs is None:
        return False

    current_month = (now or datetime.now(UTC)).month
    if current_month not in troughs:
        return False

    if len(month_buckets) < _MIN_BASELINE_MONTHS + 3:
        # Not enough history to compute a baseline distinct from the
        # last-3 window. Refuse to call it seasonal rather than guess.
        return False

    last3 = month_buckets[-3:]
    # Trailing-12-month baseline. Falls back to "everything before
    # the last-3 window" when fewer than 12 months exist.
    baseline_end = len(month_buckets) - 3
    baseline_start = max(0, baseline_end - 12)
    baseline = month_buckets[baseline_start:baseline_end]
    if len(baseline) < _MIN_BASELINE_MONTHS:
        return False

    last3_avg = _avg_deposits(last3)
    baseline_avg = _avg_deposits(baseline)
    if baseline_avg <= 0:
        return False

    dip = (baseline_avg - last3_avg) / baseline_avg
    return dip > _SEASONAL_DIP_THRESHOLD


__all__ = ["is_seasonal_trough"]
