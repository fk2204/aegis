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
from typing import TYPE_CHECKING
from uuid import UUID

from aegis.counterparty.models import CounterpartyClassification
from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import (
    detect_impossible_payment_load,
    detect_stated_vs_measured_revenue_divergence,
)
from aegis.scoring_v2.aggregation import aggregate_bundle
from aegis.scoring_v2.industry import IndustryTier, industry_tier_reason

if TYPE_CHECKING:
    # Type-only import so this module stays decoupled from
    # aegis.merchants. ``compute_risk_band`` accepts the merchant via an
    # optional kwarg; the runtime detector reads its fields via
    # ``getattr`` so the call works against any object shape.
    from aegis.merchants.models import MerchantRow
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
    BandLevel,
    BusinessRiskBand,
    CashflowSignals,
    FactorReason,
    SignalSeverity,
)
from aegis.scoring_v2.track_b.signals import (
    compute_international_share_pct,
    compute_mca_position_breakdown,
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
    "concern": 2,
    "neutral": 3,
    "positive": 4,
}


# Band ordering for industry-tier adjustment. ``low < moderate <
# elevated < high``; ``apply_industry_tier_adjustment`` clamps at
# ``high`` so two ``+1`` bumps don't wrap.
_BAND_ORDER: list[BandLevel] = ["low", "moderate", "elevated", "high"]


def apply_industry_tier_adjustment(
    cashflow_band: BandLevel,
    industry_tier: IndustryTier,
) -> BandLevel:
    """Adjust the cashflow-only band per the industry tier.

    * ``standard`` / ``moderate``     -> no change
    * ``elevated``                    -> bump one step (capped at ``high``)
    * ``high_volatility``             -> bump two steps (capped at ``high``)
    * ``hard_decline_class``          -> force ``high`` regardless of input

    Shadow-only per CLAUDE.md "Decision-boundary changes — shadow-
    first": the band itself doesn't drive the live decline path, so
    industry adjustment is informational. ``hard_decline_class`` is
    the strongest signal (force-to-high) but still goes through the
    same shadow envelope; the operator decides whether the deal
    proceeds.
    """
    if industry_tier == "hard_decline_class":
        return "high"
    idx = _BAND_ORDER.index(cashflow_band)
    if industry_tier == "high_volatility":
        idx = min(idx + 2, len(_BAND_ORDER) - 1)
    elif industry_tier == "elevated":
        idx = min(idx + 1, len(_BAND_ORDER) - 1)
    return _BAND_ORDER[idx]


# Map industry tier -> severity for the FactorReason emitted on the
# Track B reasons list. Mirrors the band-adjustment semantics:
# ``standard`` is a positive signal, ``moderate`` is neutral noise,
# ``elevated``/``high_volatility``/``hard_decline_class`` escalate.
_INDUSTRY_TIER_SEVERITY: dict[IndustryTier, SignalSeverity] = {
    "standard": "positive",
    "moderate": "neutral",
    "elevated": "elevated",
    "high_volatility": "critical",
    "hard_decline_class": "critical",
}


def compute_risk_band(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
    classifications: Mapping[UUID, CounterpartyClassification],
    *,
    industry_tier: IndustryTier | None = None,
    merchant: MerchantRow | None = None,
) -> BusinessRiskBand:
    """Compute the Track B band for one parse bundle.

    Returns a ``BusinessRiskBand`` with the band, the
    operator-action mapping, the underlying cashflow numbers, and
    the per-factor reasons that explain the band.

    The function is deterministic, has no I/O, and reads no scoring
    config — fully unit-testable. The band-from-signals composition
    is the entire logic; threshold tuning lives in ``banding.py``.

    ``industry_tier`` (kwarg) is the merchant's industry risk class
    from ``aegis.scoring_v2.industry.industry_risk_tier``. When
    provided, applies the band adjustment per
    :func:`apply_industry_tier_adjustment` AFTER the cashflow band
    is derived, and adds a FactorReason naming the tier. ``None``
    skips the adjustment entirely (legacy callers / tests that
    haven't been threaded with industry data).

    ``merchant`` (kwarg) is the ``MerchantRow`` whose stated /
    application figures (``stated_daily_payment``, ``monthly_revenue``)
    are cross-checked against the bank-measured numbers. When supplied
    AND the merchant carries the relevant ``stated_*`` field, two
    detectors fire:

      * ``impossible_payment_load`` (severity ``critical``) — stated
        daily payment x 22 business days > measured monthly revenue x 1.5.
        Catches the "Vibration Guys" failure mode where the merchant's
        existing obligations exceed their cashflow capacity.
      * ``stated_vs_measured_revenue_divergence`` (severity ``elevated``)
        — application revenue diverges > 40% from bank-measured.
        Catches misrepresentation BEFORE submission.

    Both detectors degrade to ``None`` (no reason emitted) when their
    inputs are missing — safe to thread the merchant unconditionally.
    """
    agg = aggregate_bundle(transactions_by_doc, classifications)

    # ── deterministic signals ──────────────────────────────────────
    period_days = compute_period_days(transactions_by_doc)
    monthly_revenue = compute_monthly_revenue(agg.revenue_total, period_days)
    adb, lowest, neg_days = compute_running_balance_stats(transactions_by_doc)
    nsf = compute_nsf_count(transactions_by_doc)
    mca = compute_mca_position_count(transactions_by_doc)
    mca_confirmed, mca_pattern = compute_mca_position_breakdown(transactions_by_doc)
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
        mca_confirmed_count=mca_confirmed,
        mca_pattern_count=mca_pattern,
        international_client_share_pct=intl_share,
    )

    # ── severity per factor + reasons ──────────────────────────────
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
    # Severity stays driven by the total mca_debit count (the existing
    # banding contract). The split feeds the reason text — confirmed
    # vs pattern visibility — without changing the band's input.
    mca_sev = severity_for_mca_positions(mca)
    severities.append(mca_sev)
    reasons.append(
        frame_mca_positions(
            mca,
            mca_sev,
            confirmed_count=mca_confirmed,
            pattern_count=mca_pattern,
        )
    )

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

    # Application-vs-measured reality checks. Both run only when a
    # merchant has been threaded AND the relevant ``stated_*`` field is
    # populated — the detector helpers return ``None`` on missing inputs
    # so this stays safe pre-Agent-2-merge. Severity hard-coded to mirror
    # the detector severity (85 = critical, 60 = elevated) so the dossier
    # ordering puts these at the top when they fire.
    if merchant is not None:
        impossible_load = detect_impossible_payment_load(merchant, monthly_revenue)
        if impossible_load is not None:
            severities.append("critical")
            reasons.append(
                FactorReason(
                    factor="impossible_payment_load",
                    severity="critical",
                    detail=impossible_load.detail,
                )
            )
        revenue_divergence = detect_stated_vs_measured_revenue_divergence(merchant, monthly_revenue)
        if revenue_divergence is not None:
            severities.append("elevated")
            reasons.append(
                FactorReason(
                    factor="stated_vs_measured_revenue_divergence",
                    severity="elevated",
                    detail=revenue_divergence.detail,
                )
            )

    # ── band + action ──────────────────────────────────────────────
    worst = worst_severity(severities)
    cashflow_band = band_from_severity(worst)

    # Industry-tier adjustment happens AFTER the cashflow-only band is
    # computed: the cashflow signals describe what the statements say,
    # the tier describes what kind of business AEGIS is looking at.
    # Mixing them at the severity layer would bury the cashflow
    # picture; keeping them separated lets the underwriter see both
    # "cashflow says X" and "industry bumps it to Y" on the dossier.
    if industry_tier is not None:
        band = apply_industry_tier_adjustment(cashflow_band, industry_tier)
        sev = _INDUSTRY_TIER_SEVERITY[industry_tier]
        # SignalSeverity import deferred — see import block above.
        reasons.append(
            FactorReason(
                factor="industry_tier",
                severity=sev,
                detail=f"Industry tier {industry_tier} — {industry_tier_reason(industry_tier)}",
            )
        )
    else:
        band = cashflow_band

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
