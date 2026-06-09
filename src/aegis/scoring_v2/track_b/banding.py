"""Severity mapping for each Track B factor + the band-from-severities
combinator.

Each factor (``true_revenue``, ``nsf``, ``mca_positions``, ...) has a
threshold table that maps its measured value to a ``SignalSeverity``.
The band is the worst severity observed; the underwriter reads the
reasons list to see WHY.

Thresholds are conservative and explainable. The numbers are tuned
to the operator's underwriting research — not derived from a black-box
optimisation. Each threshold change is a deliberate code edit (with
the rationale in the diff), never a hidden tuning to clear a specific
deal (CLAUDE.md "no track-tuning to pass a specific merchant" rule).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from aegis.money import Money
from aegis.scoring_v2.track_b.models import (
    SEVERITY_TO_BAND,
    BandLevel,
    SignalSeverity,
)

# ─────────────────────────────────────────────────────────────────────
# Per-factor severity thresholds
# ─────────────────────────────────────────────────────────────────────


# Monthly revenue thresholds (post-counterparty-foundation; true
# revenue, not gross inflow). Tuned to typical MCA deal sizes:
# Deals targeting $50K+ advance need $25K+ monthly revenue minimum;
# below that the deal economics get thin.
_REVENUE_STRONG_FLOOR: Final[Money] = Money(Decimal("50000"))
_REVENUE_OK_FLOOR: Final[Money] = Money(Decimal("25000"))
_REVENUE_THIN_FLOOR: Final[Money] = Money(Decimal("10000"))


def severity_for_monthly_revenue(monthly_revenue: Money) -> SignalSeverity:
    """Higher revenue is better; conservative thresholds for MCA deals.

    Examples:
        $0       → critical (no revenue)
        $5K      → elevated (genuinely thin)
        $15K     → concern (small but workable)
        $40K     → neutral (solid)
        $100K    → positive (strong)
    """
    if monthly_revenue <= Money(Decimal("0")):
        return "critical"
    if monthly_revenue < _REVENUE_THIN_FLOOR:
        return "elevated"
    if monthly_revenue < _REVENUE_OK_FLOOR:
        return "concern"
    if monthly_revenue < _REVENUE_STRONG_FLOOR:
        return "neutral"
    return "positive"


def severity_for_nsf(nsf_count: int, statement_period_days: int) -> SignalSeverity:
    """NSF frequency is a classical distress signal.

    Normalised against statement-period length so a 4-month bundle
    isn't penalised vs a 1-month one. NSF rate / month:
        0      → positive (no NSFs)
        < 1/mo → neutral  (occasional, not a pattern)
        1-2/mo → concern
        3-5/mo → elevated
        >5/mo  → critical
    """
    if nsf_count == 0:
        return "positive"
    if statement_period_days <= 0:
        # Defensive — can't normalise; treat raw count as monthly.
        nsf_per_month = Decimal(nsf_count)
    else:
        nsf_per_month = (
            Decimal(nsf_count) * Decimal("30") / Decimal(statement_period_days)
        )
    if nsf_per_month < Decimal("1"):
        return "neutral"
    if nsf_per_month < Decimal("3"):
        return "concern"
    if nsf_per_month < Decimal("5"):
        return "elevated"
    return "critical"


def severity_for_mca_positions(mca_position_count: int) -> SignalSeverity:
    """Stacking — the LOAD-BEARING default-risk signal (~40% of
    defaults link to stacking).

    Raw mca_debit counts are a coarse proxy; the band logic counts
    any presence as concerning, with sharper thresholds for multiple
    positions. Underwriter cross-references against named funders to
    count distinct positions, but the parser's count alone is enough
    for the band trigger.

    Severity tiers per total mca_debit count over the bundle:
        0   → positive (no detected MCA debits)
        1   → concern  (one position — workable but flagged)
        2-4 → elevated (multi-position stacking)
        >=5  → critical (heavy stacking — strong default signal)
    """
    if mca_position_count == 0:
        return "positive"
    if mca_position_count == 1:
        return "concern"
    if mca_position_count < 5:
        return "elevated"
    return "critical"


def severity_for_negative_days(negative_days: int) -> SignalSeverity:
    """Days in the red over the statement period.

    The underwriter mainly cares about NSFs + lowest balance; the
    negative-day count is a coarse proxy that often co-fires with
    those. Treat conservatively.

        0      → positive
        1-3    → neutral  (occasional dip)
        4-10   → concern  (frequent dipping below zero)
        >10    → elevated (chronically negative cashflow)
    """
    if negative_days == 0:
        return "positive"
    if negative_days <= 3:
        return "neutral"
    if negative_days <= 10:
        return "concern"
    return "elevated"


def severity_for_lowest_balance(lowest_balance: Money) -> SignalSeverity:
    """Depth of the worst negative point.

        >= $0      → positive (never negative)
        > -$1K     → neutral (transient dip)
        > -$5K     → concern (notable negative)
        > -$15K    → elevated (deep negative)
        ≤ -$15K    → critical (deep distress)
    """
    if lowest_balance >= Money(Decimal("0")):
        return "positive"
    if lowest_balance > Money(Decimal("-1000")):
        return "neutral"
    if lowest_balance > Money(Decimal("-5000")):
        return "concern"
    if lowest_balance > Money(Decimal("-15000")):
        return "elevated"
    return "critical"


# Concentration thresholds. Track C surfaces concentration as
# DURABILITY, not fraud; Track B reads it as a BAND-MODIFYING factor
# — high concentration nudges the band up by one notch from
# cashflow-only signals.
#
# cite: docs/AEGIS_MASTER_PLAN.md §6.4 (`customer_concentration`
#   detector — "One counterparty >30% of revenue") and the §5 Industry
#   Statement-Analysis Standards row ("Largest deposit % of revenue —
#   >30% from one source = customer concentration"). The 30% floor
#   also matches Track C's `_DURABILITY_SHARE_FLOOR_PCT`
#   (src/aegis/scoring_v2/track_c/framing.py:42) so the two tracks
#   stay aligned: below 30% the concentration is informational only.
# rationale: 30% = floor at which a single international counterparty
#   becomes a meaningful durability question — would the merchant
#   survive losing that one revenue stream? Below this, Track C marks
#   the row `info` and Track B treats it as `neutral`.
# Re-validate quarterly per the research-currency note in
#   docs/SCORING_REDESIGN_CONTINUATION.md:20 (baseline 2026-06-04).
_INTERNATIONAL_CONCENTRATION_CONCERN: Final[Decimal] = Decimal("30")

# cite: docs/AEGIS_MASTER_PLAN.md §6.4 — `customer_concentration`
#   detector severity ladder ("31-40 = mild (10), 41-60 = moderate
#   (20), 61+ = severe (30)"), echoed in
#   src/aegis/web/_pattern_cards.py:226-231 ("severity 10 / 20 / 30 at
#   30% / 40% / 60%") and in src/aegis/scoring_v2/track_c/__init__.py:22
#   ("if 60% of revenue is from one named end customer, the deal lives
#   or dies on that one relationship").
# rationale: 60% = the operator's "elevated" trigger for international
#   concentration risk; above this the band sits at `elevated`
#   regardless of cashflow strength. Capped at `elevated` (never
#   `critical`) because international wires are revenue, not fraud
#   — see severity_for_international_concentration docstring below.
# Re-validate quarterly per the research-currency note in
#   docs/SCORING_REDESIGN_CONTINUATION.md:20 (baseline 2026-06-04).
_INTERNATIONAL_CONCENTRATION_ELEVATED: Final[Decimal] = Decimal("60")


def severity_for_international_concentration(share_pct: Decimal) -> SignalSeverity:
    """International concentration → durability question.

    Never ``critical`` (that would be auto-decline territory, which
    Track C's reframe explicitly forbids — international wires are
    revenue, not fraud). Caps at ``elevated`` so the band can still
    be ``elevated`` from concentration alone but never ``high``.
    """
    if share_pct < _INTERNATIONAL_CONCENTRATION_CONCERN:
        return "neutral"
    if share_pct < _INTERNATIONAL_CONCENTRATION_ELEVATED:
        return "concern"
    return "elevated"


# ─────────────────────────────────────────────────────────────────────
# Band-from-severities combinator
# ─────────────────────────────────────────────────────────────────────


_SEVERITY_ORDER: Final[dict[SignalSeverity, int]] = {
    "positive": 0,
    "neutral":  1,
    "concern":  2,
    "elevated": 3,
    "critical": 4,
}


def worst_severity(
    severities: list[SignalSeverity],
) -> SignalSeverity:
    """Return the worst severity in the list. ``positive`` when empty
    so a bundle with no measurable factors lands ``low`` rather than
    erroring (the insufficient-data list surfaces what was missing)."""
    if not severities:
        return "positive"
    return max(severities, key=lambda s: _SEVERITY_ORDER[s])


def band_from_severity(severity: SignalSeverity) -> BandLevel:
    return SEVERITY_TO_BAND[severity]


__all__ = [
    "band_from_severity",
    "severity_for_international_concentration",
    "severity_for_lowest_balance",
    "severity_for_mca_positions",
    "severity_for_monthly_revenue",
    "severity_for_negative_days",
    "severity_for_nsf",
    "worst_severity",
]
