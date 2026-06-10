"""Track A historical lookback — confirm Track A would catch every
legacy ``fraud_score_critical`` hard-decline.

Plan 4.1 (the first Step-2 cutover gate). DEFAULT MODE: read-only.
Zero writes. Walks every document in the corpus whose ``fraud_score``
meets the legacy hard-decline threshold (default 65, the
``aegis.parser.pipeline.HARD_DECLINE_THRESHOLD`` value), reconstructs
the per-document integrity signals from already-persisted data
(``fraud_score_breakdown["metadata"]``, ``metadata_flags``, and the
``[MATH]`` entries on ``all_flags``), and runs Track A's
``compute_integrity_verdict``.

Reports each document as a CSV row to stdout:

  merchant_id, document_id, original_fraud_score,
  metadata_score, math_score, legacy_would_decline,
  track_a_verdict, track_a_branch, miss

A row is a **miss** when the legacy rule would have declined
(``fraud_score >= threshold``) but Track A produced a non-``fail``
verdict. Misses are the operator-triage items: each one is either
(a) a genuine regression Track A doesn't catch, (b) a detector gap
worth patching, or (c) a corpus-shape artifact. The script does NOT
categorise — that's the operator's call (see ``docs/REMAINING_WORK.md``
Wave 4.1 gating conditions).

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
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from aegis.parser.pipeline import HARD_DECLINE_THRESHOLD
from aegis.scoring_v2.track_a import (
    DocumentIntegritySignals,
    IntegrityVerdict,
    compute_integrity_verdict,
)
from aegis.storage import DocumentRow, ParseStatus

# Exit codes — keep aligned with the shadow-comparison script.
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_MISSES_PRESENT: Final[int] = 3


# ─────────────────────────────────────────────────────────────────────
# Pure-function core — testable in isolation
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LookbackRow:
    """One document's lookback result.

    ``is_miss`` is the gate the exit code reads. A miss = the legacy
    rule declines but Track A would not. Both ``False`` flags ("legacy
    didn't decline" or "Track A correctly fails") produce ``is_miss=False``.
    """

    merchant_id: str
    document_id: str
    original_fraud_score: int
    metadata_score: int
    math_score: int
    legacy_would_decline: bool
    track_a_verdict: str
    track_a_branch: str
    is_miss: bool


def _extract_math_failures(all_flags: list[str]) -> tuple[str, ...]:
    """Pull the validation failures from a document's persisted flag list.

    Flags are prefixed by the parser as ``[META] ...``, ``[MATH] ...``,
    ``[WARN] ...``, ``[PATTERN] ...``, etc. (see
    ``_collect_flags`` in ``aegis.parser.pipeline``). Track A's
    composition reads the math/validation failure codes verbatim; we
    strip the ``[MATH] `` prefix and forward the rest.
    """
    return tuple(
        f[len("[MATH] "):]
        for f in all_flags
        if isinstance(f, str) and f.startswith("[MATH] ")
    )


def _integrity_signals_from_document(doc: DocumentRow) -> DocumentIntegritySignals:
    """Reconstruct Track A's input shape from a persisted DocumentRow."""
    breakdown = doc.fraud_score_breakdown or {}
    metadata_score = int(breakdown.get("metadata", 0))
    return DocumentIntegritySignals(
        document_id=str(doc.id),
        metadata_score=metadata_score,
        metadata_flags=tuple(doc.metadata_flags or []),
        validation_failures=_extract_math_failures(doc.all_flags or []),
    )


def evaluate_document(
    doc: DocumentRow, *, threshold: int = HARD_DECLINE_THRESHOLD
) -> LookbackRow:
    """Compute the lookback row for one document.

    Pure — no DB access. Useful for unit tests and for any caller that
    already has the DocumentRow in memory.

    ``legacy_would_decline`` mirrors the parser pipeline's gate:
    ``fraud_score >= HARD_DECLINE_THRESHOLD``.

    ``is_miss`` is ``True`` only when the legacy rule declines AND
    Track A's verdict is NOT ``fail`` — i.e. Step 2 would let the
    deal through where the legacy rule wouldn't.
    """
    breakdown = doc.fraud_score_breakdown or {}
    metadata_score = int(breakdown.get("metadata", 0))
    math_score = int(breakdown.get("math", 0))
    legacy_would_decline = (doc.fraud_score or 0) >= threshold

    verdict: IntegrityVerdict = compute_integrity_verdict(
        _integrity_signals_from_document(doc)
    )

    is_miss = legacy_would_decline and verdict.verdict != "fail"

    return LookbackRow(
        merchant_id=str(doc.merchant_id) if doc.merchant_id else "",
        document_id=str(doc.id),
        original_fraud_score=int(doc.fraud_score or 0),
        metadata_score=metadata_score,
        math_score=math_score,
        legacy_would_decline=legacy_would_decline,
        track_a_verdict=verdict.verdict,
        track_a_branch=verdict.branch,
        is_miss=is_miss,
    )


# ─────────────────────────────────────────────────────────────────────
# Repository adapter — wraps the prod / in-memory DocumentRepository
# ─────────────────────────────────────────────────────────────────────


class _DocSource(Protocol):
    """Minimal contract the lookback consumes.

    Both ``SupabaseDocumentRepository`` and ``InMemoryDocumentRepository``
    satisfy it via their ``list_documents`` method (limit-bounded,
    most-recent first). Signature mirrors ``DocumentRepository.list_documents``
    so a real repo is structurally a ``_DocSource``.
    """

    def list_documents(
        self,
        *,
        parse_status: ParseStatus | None = None,
        merchant_id: UUID | None = None,
        limit: int = 100,
    ) -> list[DocumentRow]: ...


def run_lookback(
    source: _DocSource, *, threshold: int = HARD_DECLINE_THRESHOLD, limit: int = 1000
) -> list[LookbackRow]:
    """Iterate documents that would have hit the legacy hard-decline
    and produce the lookback rows.

    Only emits rows for documents where ``legacy_would_decline`` —
    the question this lookback answers is "did Track A catch what
    the legacy rule caught?", not "did Track A agree on clean deals."

    The limit is hard. A future paginated variant can lift it if the
    operator's corpus grows past the cap.
    """
    rows: list[LookbackRow] = []
    for doc in source.list_documents(limit=limit):
        evaluated = evaluate_document(doc, threshold=threshold)
        if evaluated.legacy_would_decline:
            rows.append(evaluated)
    return rows


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
        rows = run_lookback(repo, threshold=args.threshold, limit=args.limit)
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
