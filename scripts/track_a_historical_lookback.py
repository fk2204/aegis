"""Track A + Track B historical lookback — confirm the track_abc
engine would catch every legacy ``fraud_score_critical`` hard-decline.

Plan 4.1 (the first Step-2 cutover gate). DEFAULT MODE: read-only.
Zero writes. Walks every document in the corpus whose ``fraud_score``
meets the legacy hard-decline threshold (default 65, the
``aegis.parser.pipeline.HARD_DECLINE_THRESHOLD`` value), reconstructs
the per-document integrity signals from already-persisted data
(``fraud_score_breakdown["metadata_score"]``, ``metadata_flags``, and
the ``[MATH]`` entries on ``all_flags``), runs Track A's
``compute_integrity_verdict``, and reconstructs Track B's band from
``fraud_score_breakdown["patterns_score"]`` + ``[PATTERN]`` flag
count.

Reports each document as a CSV row to stdout:

  merchant_id, document_id, original_fraud_score,
  metadata_score, math_score, legacy_would_decline,
  track_a_verdict, track_a_branch, track_b_band, miss

A row is a **miss** when ALL THREE conditions hold:

  * legacy_would_decline (``fraud_score >= threshold``)
  * Track A's verdict is NOT ``"fail"`` — i.e. integrity is clean / review
  * Reconstructed Track B band is NOT ``"high"``

Misses are the operator-triage items under the ``track_abc`` engine
(both Track A AND Track B let the deal through): each one is either
(a) a genuine regression neither track catches, (b) a detector gap
worth patching, or (c) a corpus-shape artifact. The script does NOT
categorise — that's the operator's call (see ``docs/REMAINING_WORK.md``
Wave 4.1 gating conditions).

Track B reconstruction discipline
---------------------------------
The lookback synthesises a Track B band from the document row alone
— no transactions read, no Bedrock call. Thresholds mirror the
legacy escalation rules at ``patterns_score >= 80`` (auto-bump above
HARD_DECLINE_THRESHOLD) and the compound ``fraud_cluster_triangulated``
rule at 4+ concurrent patterns. The reconstruction is coarse by
design — decision-quality only for the ``high vs not`` gate, not for
diagnostic Track B reporting. The dossier panel runs
``compute_risk_band`` directly with full transaction context.

Exit codes (mirror ``shadow_comparison_a_b_c_vs_fraud_score.py``):
  0 — no misses (Track A caught everything the legacy rule did).
  1 — runtime error (DB unreachable, settings missing, etc.).
  3 — at least one miss row (REGRESSION — operator triage required).

Run on the box, with ``/etc/aegis/aegis.env`` sourced::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/track_a_historical_lookback.py
    .venv/bin/python scripts/track_a_historical_lookback.py --threshold 65
    .venv/bin/python scripts/track_a_historical_lookback.py --limit 500

This script lives at ``scripts/`` (flat) alongside the sibling read-only
diagnostics like ``shadow_comparison_a_b_c_vs_fraud_score.py``.
``scripts/audit/`` is reserved for prod-WRITE / side-effect / external-
API-cost scripts; this lookback has neither.
"""

from __future__ import annotations

import argparse
import csv
import sys
import traceback
from typing import Final

from aegis.parser.pipeline import HARD_DECLINE_THRESHOLD
from aegis.scoring_v2.track_a.lookback import (
    LookbackRow,
    _count_pattern_flags,
    _DocSource,
    _extract_math_failures,
    _integrity_signals_from_document,
    _read_score_component,
    _reconstruct_track_b_band,
    evaluate_document,
    run_lookback,
)

# Exit codes — keep aligned with the shadow-comparison script.
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_MISSES_PRESENT: Final[int] = 3

# Re-exports kept for backwards compatibility with existing tests at
# ``tests/scripts/test_track_a_historical_lookback.py`` and any operator
# muscle memory using the script-level import paths. The implementation
# lives in ``aegis.scoring_v2.track_a.lookback`` as of 2026-06-24 (Wave
# 4.2 — weekly arq-cron wiring).
__all__ = [
    "EXIT_MISSES_PRESENT",
    "EXIT_OK",
    "EXIT_RUNTIME_ERROR",
    "LookbackRow",
    "_DocSource",
    "_count_pattern_flags",
    "_extract_math_failures",
    "_integrity_signals_from_document",
    "_read_score_component",
    "_reconstruct_track_b_band",
    "evaluate_document",
    "main",
    "run_lookback",
    "write_csv",
]


# ─────────────────────────────────────────────────────────────────────
# CSV output
# ─────────────────────────────────────────────────────────────────────


_CSV_HEADER: Final[tuple[str, ...]] = (
    "merchant_id",
    "document_id",
    "original_fraud_score",
    "metadata_score",
    "math_score",
    "legacy_would_decline",
    "track_a_verdict",
    "track_a_branch",
    "track_b_band",
    "miss",
)


def write_csv(rows: list[LookbackRow], stream: object) -> None:
    """Write the CSV representation of the lookback rows.

    Header on row 1. Booleans serialised as ``"true"`` / ``"false"``
    for grep-friendliness from the shell.
    """
    writer = csv.writer(stream)  # type: ignore[arg-type]
    writer.writerow(_CSV_HEADER)
    for r in rows:
        writer.writerow(
            (
                r.merchant_id,
                r.document_id,
                r.original_fraud_score,
                r.metadata_score,
                r.math_score,
                "true" if r.legacy_would_decline else "false",
                r.track_a_verdict,
                r.track_a_branch,
                r.track_b_band,
                "true" if r.is_miss else "false",
            )
        )


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Track A historical lookback — confirm Track A would catch "
            "every legacy fraud_score_critical hard-decline. Read-only."
        )
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=HARD_DECLINE_THRESHOLD,
        help=(
            "Legacy hard-decline threshold to scan above (default: "
            f"{HARD_DECLINE_THRESHOLD}, the parser HARD_DECLINE_THRESHOLD). "
            "Lower values widen the sweep, useful for ablation; higher "
            "values restrict to the most-severe legacy declines."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Document scan cap (default: 1000).",
    )
    p.add_argument(
        "--skip-orphans",
        action="store_true",
        help=(
            "Skip documents whose merchant_id is NULL. Track A reasoning "
            "depends on merchant context (industry, stack, monthly buckets); "
            "an orphan can't be meaningfully evaluated and reading it as a "
            "regression is false-positive noise. The cutover gate enables "
            "this; ad-hoc audits leave it off so orphans still surface."
        ),
    )
    return p.parse_args()


def _load_repository() -> _DocSource:
    """Lazy import so unit tests that exercise the pure functions don't
    require Supabase env vars to be present."""
    from aegis.storage import SupabaseDocumentRepository

    return SupabaseDocumentRepository()


def main() -> int:
    args = _parse_args()
    try:
        repo = _load_repository()
    except Exception as exc:
        # Top-level CLI guard — surface any init failure with the
        # exit-1 contract documented in the module docstring, rather
        # than letting a Supabase/env error crash the user-facing CLI.
        print(f"ERROR: could not initialise document repository: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    try:
        rows = run_lookback(
            repo,
            threshold=args.threshold,
            limit=args.limit,
            skip_orphans=args.skip_orphans,
        )
    except Exception as exc:
        # Same posture as the init guard above — keep the CLI exit-code
        # contract intact regardless of which layer raised.
        print(f"ERROR: lookback failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    write_csv(rows, sys.stdout)

    miss_count = sum(1 for r in rows if r.is_miss)
    total_legacy_declines = sum(1 for r in rows if r.legacy_would_decline)
    print(
        f"# scanned legacy declines: {total_legacy_declines}; "
        f"misses (Track A would not fail): {miss_count}",
        file=sys.stderr,
    )
    return EXIT_MISSES_PRESENT if miss_count > 0 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
