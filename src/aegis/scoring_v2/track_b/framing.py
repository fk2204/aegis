"""Underwriter-voice reasoning copy for Track B factors.

Each factor's ``FactorReason.detail`` reads like the underwriter
themselves wrote it — concrete numbers, no jargon, what-this-means
language. Lives as code (not template) for the same reasons
Track C's framing.py does: changes are code-reviewable and
consistent across the dossier, API, and PDF surfaces.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.money import Money
from aegis.scoring_v2.track_b.models import FactorReason, SignalSeverity


def frame_monthly_revenue(monthly_revenue: Money, severity: SignalSeverity) -> FactorReason:
    detail = (
        f"True revenue averages ${monthly_revenue:,.0f}/month "
        "(net of own-account transfers, book wires, and card paydowns). "
    )
    if severity == "positive":
        detail += "Strong for typical MCA deal sizes."
    elif severity == "neutral":
        detail += "Solid and workable."
    elif severity == "concern":
        detail += (
            "Modest; deal economics get thin below $25K/month — verify remittance can be sustained."
        )
    elif severity == "elevated":
        detail += "Genuinely thin — under $10K/month limits deal viability."
    else:  # critical
        detail = (
            "No measurable revenue across counterparty classes. Either "
            "the bundle is incomplete or the business has no inflow — "
            "underwriter confirms with the merchant."
        )
    return FactorReason(factor="true_revenue", severity=severity, detail=detail)


def frame_nsf(
    nsf_count: int,
    statement_period_days: int,
    severity: SignalSeverity,
) -> FactorReason:
    if nsf_count == 0:
        detail = f"No NSF fees observed across {statement_period_days}-day statement period."
    else:
        days = max(statement_period_days, 30)
        per_mo = (Decimal(nsf_count) * Decimal("30") / Decimal(days)).quantize(Decimal("0.1"))
        detail = f"{nsf_count} NSF fees over {statement_period_days} days (~{per_mo}/month)."
        if severity == "concern":
            detail += " Occasional — cross-reference balance trace."
        elif severity == "elevated":
            detail += " Frequent — indicates recurring distress, not isolated events."
        elif severity == "critical":
            detail += " High frequency — chronic NSF pattern, treat as strong distress."
    return FactorReason(factor="nsf", severity=severity, detail=detail)


def frame_mca_positions(
    mca_position_count: int,
    severity: SignalSeverity,
    *,
    confirmed_count: int | None = None,
    pattern_count: int | None = None,
) -> FactorReason:
    """Render the MCA-position factor reason.

    ``confirmed_count`` and ``pattern_count`` (when both provided)
    surface the buckets separately per the operator's 2026-06-26 spec —
    confirmed = KNOWN_FUNDERS match, pattern = no named funder. The
    text NEVER adds them together into one count; the underwriter sees
    "N confirmed (funder name detected); M possible via payment pattern
    (verify)" so a single named funder + four generic-pattern hits no
    longer reads as "5 stacking" but as "1 confirmed + 4 possible".

    Legacy callers (no breakdown) fall back to the historical wording
    so test surfaces that don't carry the split still render cleanly.
    """
    if mca_position_count == 0:
        detail = (
            "No MCA debit transactions detected by the parser — no "
            "active position the parser could see. Underwriter still "
            "cross-references operator-known stacks."
        )
    elif confirmed_count is not None and pattern_count is not None:
        # Split-aware rendering per 2026-06-26 operator spec.
        parts: list[str] = []
        if confirmed_count > 0:
            parts.append(
                f"{confirmed_count} confirmed MCA position"
                f"{'' if confirmed_count == 1 else 's'} "
                "(funder name detected)"
            )
        if pattern_count > 0:
            parts.append(
                f"{pattern_count} possible via payment pattern "
                f"({'verify' if pattern_count == 1 else 'verify each'})"
            )
        if not parts:
            # Defensive — mca_position_count > 0 but neither bucket
            # populated means the breakdown was passed as 0/0; fall back.
            parts.append(f"{mca_position_count} MCA debit observed")
        detail = "; ".join(parts) + "."
        if confirmed_count >= 2 and severity == "elevated":
            detail += " Multi-position confirmed stacking — strong default signal."
        elif confirmed_count >= 3 and severity == "critical":
            detail += " Heavy confirmed stacking — treat as primary risk driver."
    elif mca_position_count == 1:
        detail = (
            f"{mca_position_count} MCA debit observed — one position. "
            "Workable but flagged; ~40% of MCA defaults link to stacking, "
            "so a second position is the threshold of concern."
        )
    else:
        detail = (
            f"{mca_position_count} MCA debit transactions observed across "
            "the bundle — multi-position stacking. Underwriter counts "
            "distinct named funders to confirm position count."
        )
        if severity == "elevated":
            detail += " Strong default signal."
        elif severity == "critical":
            detail += " Heavy stacking — treat as primary risk driver."
    return FactorReason(factor="mca_positions", severity=severity, detail=detail)


def frame_negative_days(negative_days: int, severity: SignalSeverity) -> FactorReason:
    if negative_days == 0:
        detail = "No days with negative running balance observed."
    elif negative_days <= 3:
        detail = (
            f"{negative_days} day(s) negative across the period — occasional dip, not a pattern."
        )
    elif negative_days <= 10:
        detail = (
            f"{negative_days} negative days — frequent dipping. "
            "Underwriter compares to deal remittance schedule."
        )
    else:
        detail = f"{negative_days} negative days — chronic negative cashflow."
    return FactorReason(factor="negative_days", severity=severity, detail=detail)


def frame_lowest_balance(lowest_balance: Money, severity: SignalSeverity) -> FactorReason:
    if severity == "positive":
        detail = f"Lowest balance observed: ${lowest_balance:,.2f}. Never went negative."
    else:
        detail = f"Lowest balance: ${lowest_balance:,.2f}. "
        if severity == "neutral":
            detail += "Transient dip below zero."
        elif severity == "concern":
            detail += "Notable negative — indicates thin reserve."
        elif severity == "elevated":
            detail += "Deep negative — distress signal."
        elif severity == "critical":
            detail += "Severe negative — strong distress."
    return FactorReason(factor="lowest_balance", severity=severity, detail=detail)


def frame_international_concentration(share_pct: Decimal, severity: SignalSeverity) -> FactorReason:
    pct = share_pct.quantize(Decimal("0.1"))
    if severity == "neutral":
        detail = (
            f"International-client concentration: {pct}%. "
            "Diversified enough that durability isn't the headline question."
        )
    elif severity == "concern":
        detail = (
            f"International-client concentration: {pct}%. "
            "Durability question — would the international counterparty(s) "
            "continue paying? NOT a fraud signal; see Track C panel for "
            "the reframe."
        )
    else:  # elevated (cap; never critical)
        detail = (
            f"International-client concentration: {pct}%. "
            "Single-counterparty durability is the load-bearing question — "
            "see Track C stress view for what remains if the counterparty "
            "drops. NOT a fraud signal."
        )
    return FactorReason(
        factor="international_concentration",
        severity=severity,
        detail=detail,
    )


__all__ = [
    "frame_international_concentration",
    "frame_lowest_balance",
    "frame_mca_positions",
    "frame_monthly_revenue",
    "frame_negative_days",
    "frame_nsf",
]
