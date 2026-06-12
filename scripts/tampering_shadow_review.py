"""Tampering shadow-rule audit-row review.

The tampering composition rule writes one ``audit_log`` row per fire:

  * shadow mode → action = ``tampering_would_decline``
  * live mode   → action = ``tampering_decline_applied``

Both share the same ``details`` shape (mode, branch, metadata_score,
math_score, contributing_failures, rationale) — see
``src/aegis/workers.py::_audit_tampering_evaluation``.

Per ``docs/REMAINING_WORK.md`` "Tampering rule shadow → live flip", the
flip is gated on operator review of shadow audit rows against known
good + known bad cases. This script is the diagnostic surface that
review reads from: pull every fire, group by branch and contributing-
failure code, surface the per-merchant / per-document distribution.

DEFAULT MODE: read-only. Zero writes. No external API calls beyond the
Supabase read. Output: CSV to stdout, summary to stderr.

Exit codes (mirror ``track_a_historical_lookback.py``):

  0 — no fires in the query window (nothing to review).
  1 — runtime error (DB unreachable, settings missing, etc.).
  3 — at least one fire row present (operator review queue is
      non-empty; not an error, just signals "look at this").

Run on the box (or from a workstation with ``DATABASE_URL`` /
Supabase creds in ``.env.local``) with the env sourced::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/tampering_shadow_review.py
    .venv/bin/python scripts/tampering_shadow_review.py --limit 200
    .venv/bin/python scripts/tampering_shadow_review.py --since 2026-06-01

Lives at ``scripts/`` (flat) alongside the sibling read-only
diagnostics like ``track_a_historical_lookback.py``. ``scripts/audit/``
is reserved for prod-WRITE / side-effect / external-API-cost scripts.
"""

from __future__ import annotations

import argparse
import csv
import sys
import traceback
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Protocol, TextIO

# Audit actions written by ``_audit_tampering_evaluation``. Both shapes
# share the ``details`` JSON keys — this script reads the union so the
# diagnostic keeps working post-flip.
SHADOW_ACTION: Final[str] = "tampering_would_decline"
LIVE_ACTION: Final[str] = "tampering_decline_applied"
TAMPERING_ACTIONS: Final[tuple[str, ...]] = (SHADOW_ACTION, LIVE_ACTION)

EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_FIRES_PRESENT: Final[int] = 3


# ─────────────────────────────────────────────────────────────────────
# Pure-function core — testable in isolation
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TamperingFire:
    """One audit row's worth of tampering-rule fire.

    Fields mirror the ``_audit_tampering_evaluation`` write shape with
    the ``rationale`` truncated for CSV legibility (full row stays in
    the audit_log table for deep dives).
    """

    document_id: str
    action: str
    mode: str
    branch: str
    metadata_score: int
    math_score: int
    contributing_failures: tuple[str, ...]
    rationale: str
    created_at: str


def parse_audit_row(row: dict[str, Any]) -> TamperingFire | None:
    """Convert one ``audit_log`` row dict into a ``TamperingFire``.

    Returns ``None`` for rows whose action isn't one of the tampering
    actions (defensive — the caller already filters server-side, but
    fixture data and operator-pasted CSVs might mix shapes).

    Missing ``details`` keys default to empty / 0 rather than raising:
    a partial audit row is still worth surfacing to the operator (the
    "this row looks malformed" signal is itself useful).
    """
    action = str(row.get("action", ""))
    if action not in TAMPERING_ACTIONS:
        return None

    details_raw = row.get("details") or {}
    if not isinstance(details_raw, dict):
        details_raw = {}

    failures_raw = details_raw.get("contributing_failures") or ()
    failures: tuple[str, ...] = tuple(str(f) for f in failures_raw) if isinstance(
        failures_raw, list | tuple
    ) else ()

    return TamperingFire(
        document_id=str(row.get("subject_id") or ""),
        action=action,
        mode=str(details_raw.get("mode") or ""),
        branch=str(details_raw.get("branch") or ""),
        metadata_score=int(details_raw.get("metadata_score") or 0),
        math_score=int(details_raw.get("math_score") or 0),
        contributing_failures=failures,
        rationale=str(details_raw.get("rationale") or ""),
        created_at=str(row.get("created_at") or ""),
    )


def parse_rows(rows: Sequence[dict[str, Any]]) -> list[TamperingFire]:
    """Map ``parse_audit_row`` across a list, dropping ``None`` results."""
    return [f for r in rows if (f := parse_audit_row(r)) is not None]


@dataclass(frozen=True)
class Summary:
    """Operator-facing rollup of a fire list.

    The branch / failure / mode counters are the primary review surface;
    ``total_fires`` and ``distinct_documents`` are the high-level signals.
    """

    total_fires: int
    distinct_documents: int
    by_action: tuple[tuple[str, int], ...]
    by_mode: tuple[tuple[str, int], ...]
    by_branch: tuple[tuple[str, int], ...]
    by_failure: tuple[tuple[str, int], ...]


def summarize(fires: Sequence[TamperingFire]) -> Summary:
    """Aggregate a fire list into branch / failure / mode rollups.

    Counters are sorted most-common-first so the review eye lands on the
    biggest cohorts first. Ties resolve by alphabetical key for
    deterministic test pinning.
    """
    actions: Counter[str] = Counter(f.action for f in fires)
    modes: Counter[str] = Counter(f.mode for f in fires)
    branches: Counter[str] = Counter(f.branch for f in fires)
    failures: Counter[str] = Counter(
        failure for f in fires for failure in f.contributing_failures
    )

    def _sort_descending(c: Counter[str]) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    return Summary(
        total_fires=len(fires),
        distinct_documents=len({f.document_id for f in fires if f.document_id}),
        by_action=_sort_descending(actions),
        by_mode=_sort_descending(modes),
        by_branch=_sort_descending(branches),
        by_failure=_sort_descending(failures),
    )


# ─────────────────────────────────────────────────────────────────────
# CSV output
# ─────────────────────────────────────────────────────────────────────


_CSV_HEADER: Final[tuple[str, ...]] = (
    "created_at",
    "action",
    "mode",
    "branch",
    "document_id",
    "metadata_score",
    "math_score",
    "contributing_failures",
    "rationale",
)


def write_csv(fires: Sequence[TamperingFire], stream: TextIO) -> None:
    """Write the CSV representation of the fire rows.

    ``contributing_failures`` serialises as a semicolon-joined string
    so the CSV stays one-row-per-fire (a parallel column-per-failure
    would explode for ops use).
    """
    writer = csv.writer(stream)
    writer.writerow(_CSV_HEADER)
    for f in fires:
        writer.writerow(
            (
                f.created_at,
                f.action,
                f.mode,
                f.branch,
                f.document_id,
                f.metadata_score,
                f.math_score,
                ";".join(f.contributing_failures),
                f.rationale,
            )
        )


def summary_lines(summary: Summary) -> list[str]:
    """Human-readable lines for stderr review.

    The order: total → action split → mode split → branch distribution
    → failure-code distribution. Mirrors how the operator reads the
    diagnostic top-down.
    """

    def _format_pairs(pairs: tuple[tuple[str, int], ...]) -> str:
        if not pairs:
            return "  (none)"
        width = max(len(k) for k, _ in pairs)
        return "\n".join(f"  {k.ljust(width)}  {v}" for k, v in pairs)

    return [
        f"total_fires: {summary.total_fires}",
        f"distinct_documents: {summary.distinct_documents}",
        "by_action:",
        _format_pairs(summary.by_action),
        "by_mode:",
        _format_pairs(summary.by_mode),
        "by_branch:",
        _format_pairs(summary.by_branch),
        "by_contributing_failure:",
        _format_pairs(summary.by_failure),
    ]


# ─────────────────────────────────────────────────────────────────────
# Supabase adapter — wraps the prod client
# ─────────────────────────────────────────────────────────────────────


class _SupabaseClient(Protocol):
    """Minimal contract this script needs.

    The real ``supabase.Client`` returned by ``aegis.db.get_supabase``
    satisfies this via its ``.table().select().in_().gte().limit().execute()``
    chain. Tests use a fake that returns a canned response.
    """

    def table(self, name: str) -> Any:  # noqa: ANN401  # supabase chain is dynamically typed
        ...


def fetch_rows(
    client: _SupabaseClient,
    *,
    since: date | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Pull recent tampering-action rows from ``audit_log``.

    Server-side narrowing:

      * ``action IN (shadow_action, live_action)``
      * optional ``created_at >= since`` (ISO date)
      * ``ORDER BY created_at DESC LIMIT <limit>``

    Returns the raw ``result.data`` list — caller maps through
    ``parse_rows`` to get ``TamperingFire`` instances.
    """
    query = (
        client.table("audit_log")
        .select("*")
        .in_("action", list(TAMPERING_ACTIONS))
    )
    if since is not None:
        query = query.gte("created_at", since.isoformat())
    result = query.order("created_at", desc=True).limit(limit).execute()
    return list(result.data or [])


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tampering_shadow_review",
        description=(
            "Tampering shadow-rule audit review — read-only. Pulls "
            "tampering_would_decline + tampering_decline_applied rows, "
            "groups by branch / failure / mode, surfaces the review "
            "queue gating the shadow -> live flip."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Server-side row cap (default: 500, ORDER BY created_at DESC).",
    )
    p.add_argument(
        "--since",
        type=date.fromisoformat,
        default=None,
        help="ISO date (YYYY-MM-DD); only rows at or after this date.",
    )
    return p.parse_args(argv)


def _load_client() -> _SupabaseClient:
    """Lazy import so unit tests don't need Supabase creds present."""
    from aegis.db import get_supabase

    return get_supabase()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        client = _load_client()
    except Exception as exc:
        print(f"ERROR: could not initialise Supabase client: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    try:
        rows = fetch_rows(client, since=args.since, limit=args.limit)
    except Exception as exc:
        print(f"ERROR: audit_log query failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    fires = parse_rows(rows)
    write_csv(fires, sys.stdout)

    for line in summary_lines(summarize(fires)):
        print(line, file=sys.stderr)

    return EXIT_FIRES_PRESENT if fires else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
