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
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from aegis.parser.pipeline import HARD_DECLINE_THRESHOLD
from aegis.scoring_v2.track_a import (
    DocumentIntegritySignals,
    IntegrityVerdict,
    compute_integrity_verdict,
)
from aegis.scoring_v2.track_b.models import BandLevel
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
    rule declines AND Track A would not fail AND Track B's
    reconstructed band is NOT ``high``. The Track B addition (2026-06-15)
    closes the false-positive where a pattern-driven legacy decline
    (e.g. preloan_spike + payroll_absent + customer_concentration)
    had Track A correctly clean on document integrity but the
    business-risk signal was already captured by Track B's high band.

    ``track_b_band`` is reconstructed from
    ``fraud_score_breakdown.patterns_score`` and the ``[PATTERN] …``
    entries on the document row — no transactions read, no Bedrock
    call. The reconstruction is intentionally lightweight and tracks
    the legacy escalation thresholds rather than mirroring
    ``compute_risk_band`` (which requires per-transaction context the
    document row does not preserve).
    """

    merchant_id: str
    document_id: str
    original_fraud_score: int
    metadata_score: int
    math_score: int
    legacy_would_decline: bool
    track_a_verdict: str
    track_a_branch: str
    track_b_band: str
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
        f[len("[MATH] ") :] for f in all_flags if isinstance(f, str) and f.startswith("[MATH] ")
    )


def _read_score_component(breakdown: dict[str, int], name: str) -> int:
    """Read one component (metadata / math / patterns) out of
    ``documents.fraud_score_breakdown``.

    ``parser.pipeline._fraud_score`` writes the canonical keys with a
    ``_score`` suffix (``"metadata_score"``, ``"math_score"``,
    ``"patterns_score"``). Prior versions of this lookback read the
    keys without the suffix and silently returned 0 for every document
    since the script's introduction; the bug surfaced when triaging
    VU DEVELOPMENT's miss (doc 49c7d058) on 2026-06-15.

    Fallback to the suffix-less key is kept so any legacy row written
    by an older code path still reads correctly. New writers must
    use the ``_score`` suffix.
    """
    return int(breakdown.get(f"{name}_score", breakdown.get(name, 0)))


# ─────────────────────────────────────────────────────────────────────
# Track B reconstruction — lightweight, document-row only
# ─────────────────────────────────────────────────────────────────────


# patterns_score thresholds used by the legacy ``_fraud_score``
# escalation rules (parser.pipeline._fraud_score lines 412-413). The
# ``>= 80`` band is the line the legacy scorer treats as severe
# enough to escalate above HARD_DECLINE_THRESHOLD on patterns alone;
# we read that as Track B ``high``. Below is interpolated to lighter
# bands so the reconstruction degrades gracefully on weaker signals.
_TRACK_B_HIGH_PATTERNS_SCORE: Final[int] = 80
_TRACK_B_ELEVATED_PATTERNS_SCORE: Final[int] = 50
_TRACK_B_MODERATE_PATTERNS_SCORE: Final[int] = 25

# When the per-component ``patterns_score`` is missing or low but the
# document carries many distinct pattern signals, that fan-out is
# itself a high-risk signal (mirrors the parser's
# ``fraud_cluster_triangulated`` compound rule which fires at 4+
# concurrent patterns). The lookback's reconstruction reflects that
# without needing the compound flag to be present.
_TRACK_B_HIGH_PATTERN_COUNT: Final[int] = 4
_TRACK_B_ELEVATED_PATTERN_COUNT: Final[int] = 2


def _count_pattern_flags(all_flags: list[str]) -> int:
    """Count entries on ``documents.all_flags`` prefixed with
    ``[PATTERN]``. The parser pipeline writes one such row per
    detector hit (preloan_spike, customer_concentration, etc.)."""
    return sum(1 for f in all_flags if isinstance(f, str) and f.startswith("[PATTERN]"))


def _reconstruct_track_b_band(doc: DocumentRow) -> BandLevel:
    """Approximate Track B's band from a persisted ``DocumentRow``.

    Reconstruction uses ONLY ``doc.fraud_score_breakdown`` and
    ``doc.all_flags`` — no transactions, no counterparty
    classifications, no Bedrock. The real ``compute_risk_band``
    needs all three and is therefore unsuitable for a historical
    lookback. The lookback's question is narrower: "would Track B
    have caught what the legacy rule caught, on the documents where
    Track A says clean?"

    Output band rules:

    * ``"high"``     — ``patterns_score >= 80`` OR pattern_count >= 4.
                       Mirrors the legacy escalation rule at
                       ``patterns_score >= 80`` (auto-bump above
                       ``HARD_DECLINE_THRESHOLD``) and the compound
                       ``fraud_cluster_triangulated`` rule at 4+
                       concurrent patterns.
    * ``"elevated"`` — ``patterns_score >= 50`` OR pattern_count >= 2.
    * ``"moderate"`` — ``patterns_score >= 25`` OR pattern_count == 1.
    * ``"low"``      — anything below.

    The thresholds are coarse by design. The reconstruction is
    decision-quality only for the ``is_miss`` gate (high vs. not),
    not for diagnostic Track B reporting. For real Track B output
    the dossier panel calls ``compute_risk_band`` directly.
    """
    breakdown = doc.fraud_score_breakdown or {}
    patterns_score = _read_score_component(breakdown, "patterns")
    pattern_count = _count_pattern_flags(doc.all_flags or [])

    if patterns_score >= _TRACK_B_HIGH_PATTERNS_SCORE:
        return "high"
    if pattern_count >= _TRACK_B_HIGH_PATTERN_COUNT:
        return "high"
    if patterns_score >= _TRACK_B_ELEVATED_PATTERNS_SCORE:
        return "elevated"
    if pattern_count >= _TRACK_B_ELEVATED_PATTERN_COUNT:
        return "elevated"
    if patterns_score >= _TRACK_B_MODERATE_PATTERNS_SCORE:
        return "moderate"
    if pattern_count >= 1:
        return "moderate"
    return "low"


def _integrity_signals_from_document(doc: DocumentRow) -> DocumentIntegritySignals:
    """Reconstruct Track A's input shape from a persisted DocumentRow."""
    breakdown = doc.fraud_score_breakdown or {}
    metadata_score = _read_score_component(breakdown, "metadata")
    return DocumentIntegritySignals(
        document_id=str(doc.id),
        metadata_score=metadata_score,
        metadata_flags=tuple(doc.metadata_flags or []),
        validation_failures=_extract_math_failures(doc.all_flags or []),
    )


def evaluate_document(doc: DocumentRow, *, threshold: int = HARD_DECLINE_THRESHOLD) -> LookbackRow:
    """Compute the lookback row for one document.

    Pure — no DB access. Useful for unit tests and for any caller that
    already has the DocumentRow in memory.

    ``legacy_would_decline`` mirrors the parser pipeline's gate:
    ``fraud_score >= HARD_DECLINE_THRESHOLD``.

    ``is_miss`` is ``True`` only when ALL THREE conditions hold:

      * legacy_would_decline
      * Track A's verdict is NOT ``"fail"``
      * Reconstructed Track B band is NOT ``"high"``

    The Track B clause closes the pattern-driven false positive — a
    deal whose legacy fraud_score escalated above the threshold from
    pattern signals (preloan_spike, customer_concentration, etc.)
    is correctly caught by Track B's ``high`` band under the
    track_abc engine, even though Track A says ``clean`` on
    document integrity.
    """
    breakdown = doc.fraud_score_breakdown or {}
    metadata_score = _read_score_component(breakdown, "metadata")
    math_score = _read_score_component(breakdown, "math")
    legacy_would_decline = (doc.fraud_score or 0) >= threshold

    verdict: IntegrityVerdict = compute_integrity_verdict(_integrity_signals_from_document(doc))
    track_b_band: BandLevel = _reconstruct_track_b_band(doc)

    is_miss = legacy_would_decline and verdict.verdict != "fail" and track_b_band != "high"

    return LookbackRow(
        merchant_id=str(doc.merchant_id) if doc.merchant_id else "",
        document_id=str(doc.id),
        original_fraud_score=int(doc.fraud_score or 0),
        metadata_score=metadata_score,
        math_score=math_score,
        legacy_would_decline=legacy_would_decline,
        track_a_verdict=verdict.verdict,
        track_a_branch=verdict.branch,
        track_b_band=track_b_band,
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
    source: _DocSource,
    *,
    threshold: int = HARD_DECLINE_THRESHOLD,
    limit: int = 1000,
    skip_orphans: bool = False,
) -> list[LookbackRow]:
    """Iterate documents that would have hit the legacy hard-decline
    and produce the lookback rows.

    Only emits rows for documents where ``legacy_would_decline`` —
    the question this lookback answers is "did Track A catch what
    the legacy rule caught?", not "did Track A agree on clean deals."

    ``skip_orphans=True`` drops documents with no ``merchant_id``
    BEFORE evaluation. An orphan has no merchant context for Track A
    to reason about (no industry tier, no monthly_breakdown, no MCA
    stack), so scoring it as a regression is noise. The cutover gate
    consumes this mode; ad-hoc audits keep the default off so the
    operator can still see orphans surface.

    The limit is hard. A future paginated variant can lift it if the
    operator's corpus grows past the cap.
    """
    rows: list[LookbackRow] = []
    for doc in source.list_documents(limit=limit):
        if skip_orphans and doc.merchant_id is None:
            continue
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
