"""Track B entry point: bundle + classifications → BusinessRiskBand.

Composition is intentionally explicit and linear so the
band-derivation reads top-to-bottom: compute signals, map each to
severity, build a reason per factor, take the worst severity as the
band, look up the action, return.

Pure function; no I/O. Same call shape as Track C
(``compute_context_panel``) — both consume the shared aggregation.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID

from aegis.counterparty.models import CounterpartyClassification
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.aggregation import aggregate_bundle
from aegis.scoring_v2.track_b.banding import (
    band_from_severity,
    severity_for_international_concentration,
    severity_for_lowest_balance,
    severity_for_mca_positions,
    severity_for_monthly_revenue,
    severity_for_negative_days,
    severity_for_nsf,
    worst_severity,
)
from aegis.scoring_v2.track_b.framing import (
    frame_international_concentration,
    frame_lowest_balance,
    frame_mca_positions,
    frame_monthly_revenue,
    frame_negative_days,
    frame_nsf,
)
from aegis.scoring_v2.track_b.models import (
    BAND_TO_ACTION,
    BusinessRiskBand,
    CashflowSignals,
    FactorReason,
)
from aegis.scoring_v2.track_b.signals import (
    compute_international_share_pct,
    compute_mca_position_count,
    compute_monthly_revenue,
    compute_nsf_count,
    compute_period_days,
    compute_running_balance_stats,
)

# Severity rank for reasons-list ordering. Worst first so the dossier
# renders the band-driving factor at the top.
_REASON_SEVERITY_RANK = {
    "critical": 0,
    "elevated": 1,
    "concern":  2,
    "neutral":  3,
    "positive": 4,
}


def compute_risk_band(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
    classifications: Mapping[UUID, CounterpartyClassification],
) -> BusinessRiskBand:
    """Compute the Track B band for one parse bundle.

    Returns a ``BusinessRiskBand`` with the band, the
    operator-action mapping, the underlying cashflow numbers, and
    the per-factor reasons that explain the band.

    The function is deterministic, has no I/O, and reads no scoring
    config — fully unit-testable. The band-from-signals composition
    is the entire logic; threshold tuning lives in ``banding.py``.
    """
    agg = aggregate_bundle(transactions_by_doc, classifications)

    # ── deterministic signals ──────────────────────────────────────
    period_days = compute_period_days(transactions_by_doc)
    monthly_revenue = compute_monthly_revenue(agg.revenue_total, period_days)
    adb, lowest, neg_days = compute_running_balance_stats(transactions_by_doc)
    nsf = compute_nsf_count(transactions_by_doc)
    mca = compute_mca_position_count(transactions_by_doc)
    intl_share = compute_international_share_pct(agg)

    cashflow = CashflowSignals(
        true_revenue_total=agg.revenue_total,
        statement_period_days=period_days,
        monthly_revenue_estimate=monthly_revenue,
        average_daily_balance=adb,
        lowest_balance=lowest,
        negative_days=neg_days,
        nsf_count=nsf,
        mca_position_count=mca,
        international_client_share_pct=intl_share,
    )

    # ── severity per factor + reasons ──────────────────────────────
    from aegis.scoring_v2.track_b.models import SignalSeverity

    reasons: list[FactorReason] = []
    severities: list[SignalSeverity] = []
    insufficient: list[str] = []

    # Revenue is always computable (zero when no revenue rows).
    rev_sev = severity_for_monthly_revenue(monthly_revenue)
    severities.append(rev_sev)
    reasons.append(frame_monthly_revenue(monthly_revenue, rev_sev))

    # NSF is always computable.
    nsf_sev = severity_for_nsf(nsf, period_days)
    severities.append(nsf_sev)
    reasons.append(frame_nsf(nsf, period_days, nsf_sev))

    # MCA is always computable.
    mca_sev = severity_for_mca_positions(mca)
    severities.append(mca_sev)
    reasons.append(frame_mca_positions(mca, mca_sev))

    # Balance-derived signals: only when running_balance coverage
    # was high enough for the stats to be returned.
    if adb is not None and lowest is not None:
        neg_sev = severity_for_negative_days(neg_days)
        severities.append(neg_sev)
        reasons.append(frame_negative_days(neg_days, neg_sev))
        low_sev = severity_for_lowest_balance(lowest)
        severities.append(low_sev)
        reasons.append(frame_lowest_balance(lowest, low_sev))
    else:
        insufficient.append("average_daily_balance")
        insufficient.append("lowest_balance")

    # International concentration: when there's any revenue. Track C
    # surfaces concentration as a separate panel; Track B reads it
    # as a band-modifying factor. Capped at ``elevated`` — never
    # ``critical`` — because concentration alone is not a fraud
    # signal (Track C's whole point).
    if agg.revenue_total > Decimal("0"):
        intl_sev = severity_for_international_concentration(intl_share)
        severities.append(intl_sev)
        reasons.append(frame_international_concentration(intl_share, intl_sev))

    # Trend / volatility: requires ≥ 4 distinct months of revenue
    # data to be meaningful. Mark insufficient when not enough; future
    # commit adds the trend signal computation.
    if period_days < 120:
        insufficient.append("trend_volatility")

    # ── band + action ──────────────────────────────────────────────
    worst = worst_severity(severities)
    band = band_from_severity(worst)
    action = BAND_TO_ACTION[band]

    # Order reasons so the band-driving factor is first.
    reasons.sort(
        key=lambda r: (
            _REASON_SEVERITY_RANK[r.severity],
            r.factor,
        )
    )

    return BusinessRiskBand(
        band=band,
        action=action,
        cashflow=cashflow,
        reasons=tuple(reasons),
        insufficient_data_factors=tuple(insufficient),
    )


__all__ = ["compute_risk_band"]
