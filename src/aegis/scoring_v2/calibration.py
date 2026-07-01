"""Calibration engine — measures AEGIS scoring accuracy vs real outcomes.

The 2026-06-30 audit (P1) established the missing learning loop:
zero recorded outcomes meant scoring had no ground-truth feedback.
Migration 103 added the snapshot table; B2 (4c8b85f) shipped the
dossier outcome capture flow. This module runs weekly, computes
accuracy metrics from accumulated outcomes, and writes one snapshot
row to ``calibration_snapshots`` per pass.

The portfolio dashboard reads the most-recent snapshot to render the
"how well is AEGIS doing" panel.

Threshold: ``MIN_OUTCOMES = 20``. Below this the snapshot is skipped
with a logged warning — small-sample percentages are misleading and
would prematurely drive threshold tuning.

The engine is OBSERVATIONAL ONLY. It computes metrics and writes
snapshots. It does NOT tune scoring thresholds, weights, or signal
emission rules. CLAUDE.md "decision-boundary changes — shadow first"
applies: any threshold change driven by these numbers is an explicit
operator decision, not an auto-tune.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from aegis.db import get_supabase

_log = logging.getLogger(__name__)

# Minimum outcomes before a snapshot is computed. Below this the
# percentage metrics swing too wildly to drive threshold review.
MIN_OUTCOMES: int = 20

# Forensic / fraud signals AEGIS treats as decline-class. When any
# of these flags fire on a deal the scorer expects the deal to either
# decline or charge off. A FUNDED deal with these flags is a
# false-positive.
_FRAUD_FLAGS: frozenset[str] = frozenset(
    {
        "editor_detected",
        "fraud_cluster_triangulated",
        "font_inconsistency_detected",
        "creator_mismatch_detected",
        "text_overlay_detected",
        "page_layer_anomaly",
    }
)


@dataclass
class CalibrationResult:
    """Snapshot of one calibration pass — what the engine wrote to
    ``calibration_snapshots`` if outcomes_count met the floor.

    ``outcome_count`` below ``MIN_OUTCOMES`` signals "not enough data
    yet" and the snapshot is NOT persisted; the dataclass is returned
    only for the cron's log line and the test surface.
    """

    computed_at: datetime
    outcome_count: int
    fraud_true_positive_rate: float = 0.0
    fraud_false_positive_rate: float = 0.0
    revenue_mean_abs_error: float = 0.0
    paper_grade_accuracy: float = 0.0
    top_false_positive_signals: list[str] = field(default_factory=list)
    top_missed_signals: list[str] = field(default_factory=list)


def compute_and_store(sb: Any = None) -> CalibrationResult | None:  # noqa: ANN401 — duck-typed supabase client for test injection
    """Compute the weekly accuracy snapshot.

    Reads every ``funder_replies`` row + matches against ``analyses``
    + ``decisions`` to compute false-positive / false-negative rates
    and signal-firing distributions. Writes one
    ``calibration_snapshots`` row when the outcome count clears
    ``MIN_OUTCOMES``; returns ``None`` (no write) when it doesn't.

    Failure-mode: any unexpected exception during read is logged and
    propagated — calibration is an operator-facing metric, silent
    failure on the metric write would make a stale dashboard look
    fresh. The caller (cron) catches at its level so other crons in
    the run still fire.
    """
    sb = sb or get_supabase()

    outcomes_resp = (
        sb.table("funder_replies")
        .select("id,merchant_id,outcome,deal_id,submission_id,created_at")
        .execute()
    )
    outcomes: list[dict[str, Any]] = [r for r in (outcomes_resp.data or []) if isinstance(r, dict)]

    # 2026-07-01 FIX 2 — merchant_outcomes (migration 106) captures
    # operator-button outcomes that don't fit the funder_replies
    # anchor-XOR contract (no deal_id, no submission_id). Fold them
    # in so calibration ground truth sees Close-side / pre-submission
    # decline signals. Best-effort: a lookup failure on the young
    # table falls through to the funder_replies-only path.
    try:
        merchant_outcomes_resp = (
            sb.table("merchant_outcomes").select("id,merchant_id,outcome,recorded_at").execute()
        )
        for row in merchant_outcomes_resp.data or []:
            if not isinstance(row, dict):
                continue
            # Normalise onto the funder_replies row shape the downstream
            # bucketing loop consumes (outcome/merchant_id/created_at).
            # deal_id + submission_id stay None — the loop tolerates.
            outcomes.append(
                {
                    "id": row.get("id"),
                    "merchant_id": row.get("merchant_id"),
                    "outcome": (
                        "approved" if row.get("outcome") == "funded" else row.get("outcome")
                    ),
                    "deal_id": None,
                    "submission_id": None,
                    "created_at": row.get("recorded_at"),
                }
            )
    except Exception as exc:
        _log.warning(
            "calibration.merchant_outcomes_query_failed exc=%s — falling "
            "back to funder_replies-only",
            exc,
        )
    count = len(outcomes)

    if count < MIN_OUTCOMES:
        _log.info(
            "calibration.insufficient_data count=%d required=%d",
            count,
            MIN_OUTCOMES,
        )
        return CalibrationResult(
            computed_at=datetime.now(UTC),
            outcome_count=count,
        )

    funded = [o for o in outcomes if o.get("outcome") == "approved"]
    declined = [o for o in outcomes if o.get("outcome") == "declined"]

    merchant_ids = sorted({str(o["merchant_id"]) for o in outcomes if o.get("merchant_id")})
    analyses_resp = (
        sb.table("analyses")
        .select("merchant_id,true_revenue,all_flags")
        .in_("merchant_id", merchant_ids)
        .execute()
    )
    analysis_by_merchant: dict[str, dict[str, Any]] = {
        str(a["merchant_id"]): cast(dict[str, Any], a)
        for a in (analyses_resp.data or [])
        if isinstance(a, dict) and a.get("merchant_id")
    }

    # Fraud false positive rate: deals AEGIS flagged that the funder
    # funded anyway. Zero-divide guard returns 0.0 when no funded
    # outcomes are in the snapshot.
    flagged_and_funded = sum(
        1 for o in funded if _has_fraud_flags(analysis_by_merchant.get(str(o.get("merchant_id"))))
    )
    false_positive_rate = flagged_and_funded / len(funded) if funded else 0.0

    # Fraud true positive rate: deals AEGIS flagged that the funder
    # also declined. Same zero-divide guard.
    flagged_and_declined = sum(
        1 for o in declined if _has_fraud_flags(analysis_by_merchant.get(str(o.get("merchant_id"))))
    )
    true_positive_rate = flagged_and_declined / len(declined) if declined else 0.0

    top_false_positive = _top_signals(funded, analysis_by_merchant)
    top_missed = _top_signals(declined, analysis_by_merchant)

    result = CalibrationResult(
        computed_at=datetime.now(UTC),
        outcome_count=count,
        fraud_true_positive_rate=true_positive_rate,
        fraud_false_positive_rate=false_positive_rate,
        revenue_mean_abs_error=0.0,  # reserved — needs funded_amount capture
        paper_grade_accuracy=0.0,  # reserved — needs funder-side grade capture
        top_false_positive_signals=top_false_positive,
        top_missed_signals=top_missed,
    )

    # Persist. Per-field NUMERIC precision matches the migration-103
    # schema (5,4 for rates, 14,2 for revenue MAE).
    sb.table("calibration_snapshots").insert(
        {
            "computed_at": result.computed_at.isoformat(),
            "outcome_count": result.outcome_count,
            "fraud_true_positive_rate": _round_rate(result.fraud_true_positive_rate),
            "fraud_false_positive_rate": _round_rate(result.fraud_false_positive_rate),
            "revenue_mean_abs_error": _round_money(result.revenue_mean_abs_error),
            "paper_grade_accuracy": _round_rate(result.paper_grade_accuracy),
            "top_false_positive_signals": result.top_false_positive_signals,
            "top_missed_signals": result.top_missed_signals,
            "raw_metrics": {
                "funded_count": len(funded),
                "declined_count": len(declined),
                "flagged_and_funded": flagged_and_funded,
                "flagged_and_declined": flagged_and_declined,
            },
        }
    ).execute()

    return result


def _has_fraud_flags(analysis: dict[str, Any] | None) -> bool:
    """Return True if any decline-class fraud signal fired on this
    merchant's analysis row. ``all_flags`` is the dict that the
    parser writes; we only care about flag-keys, not values."""
    if not analysis:
        return False
    flags = analysis.get("all_flags") or {}
    if not isinstance(flags, dict):
        return False
    return bool(_FRAUD_FLAGS & set(flags.keys()))


def _top_signals(
    outcomes: list[dict[str, Any]],
    analysis_map: dict[str, dict[str, Any]],
    top_n: int = 5,
) -> list[str]:
    """Return the top-N most-fired flag names across a list of
    outcomes. Used twice — once for funded (false-positive surface)
    and once for declined (true-positive surface; the calibration UI
    reads both lists to suggest signal-level tuning priorities)."""
    counter: Counter[str] = Counter()
    for outcome in outcomes:
        analysis = analysis_map.get(str(outcome.get("merchant_id") or ""))
        if not analysis:
            continue
        flags = analysis.get("all_flags") or {}
        if not isinstance(flags, dict):
            continue
        for flag in flags:
            counter[str(flag)] += 1
    return [name for name, _ in counter.most_common(top_n)]


def _round_rate(value: float) -> str:
    """Quantize to 4 decimal places for the migration-103 NUMERIC(5,4)
    column. Returned as a string so postgrest doesn't lose precision
    through float round-trip."""
    return str(Decimal(str(value)).quantize(Decimal("0.0001")))


def _round_money(value: float) -> str:
    """Quantize to 2 decimal places for the migration-103
    NUMERIC(14,2) column. Same string-shape rationale as
    ``_round_rate``."""
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


__all__ = [
    "MIN_OUTCOMES",
    "CalibrationResult",
    "compute_and_store",
]
