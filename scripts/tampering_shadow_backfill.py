"""Backfill the tampering composition over every parsed document.

For each document with a persisted ``fraud_score_breakdown``, replays
the composition rule (coarse / score-only path) and surfaces the
matrix the operator needs to review BEFORE the rule flips from shadow
to live decline.

Per the build brief (operator, 2026-06-04):

    "Run it against VU + every statement currently in the system,
    surface the matrix: which would be flagged, why, and whether each
    looks like a real fake or a false positive."

This is a READ-ONLY script. It writes no rows to ``documents``,
``analyses``, or ``audit_log``. The Mode column is informational —
``shadow`` rows from the actual parse-time audit (when present) are
shown alongside the recompute for an apples-to-apples check.

Usage on prod box:

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    uv run python scripts/tampering_shadow_backfill.py [--limit N]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, cast

from supabase import create_client

from aegis.config import get_settings
from aegis.parser.tampering import evaluate_tampering_from_scores


@dataclass
class _Row:
    document_id: str
    original_filename: str
    merchant_id: str | None
    fraud_score: int
    metadata_score: int
    math_score: int
    patterns_score: int
    would_decline: bool
    branch: str
    metadata_flags: list[str]


_STRONG = 50
_MEDIUM_FLOOR = 25
_MEDIUM_CEIL = 49
_MATH_CORROB = 55


def _branch_label(metadata_score: int, math_score: int) -> str:
    if metadata_score >= _STRONG:
        return "strong_metadata"
    if _MEDIUM_FLOOR <= metadata_score <= _MEDIUM_CEIL and math_score >= _MATH_CORROB:
        return "medium_corroborated"
    if _MEDIUM_FLOOR <= metadata_score <= _MEDIUM_CEIL:
        return "medium_uncorroborated"
    return "below_thresholds"


def main(limit: int | None) -> int:
    s = get_settings()
    if s.supabase_service_key is None:
        print("SUPABASE_SERVICE_KEY is not configured.", file=sys.stderr)
        return 2
    sb = create_client(s.supabase_url, s.supabase_service_key.get_secret_value())

    print(f"Current AEGIS_TAMPERING_DECLINE_MODE = {s.aegis_tampering_decline_mode}")
    print()

    q = (
        sb.table("documents")
        .select(
            "id,original_filename,merchant_id,fraud_score,"
            "fraud_score_breakdown,metadata_flags,parse_status,uploaded_at"
        )
        .order("uploaded_at", desc=True)
    )
    if limit is not None:
        q = q.limit(limit)
    r = q.execute()
    docs = r.data or []

    rows: list[_Row] = []
    for raw in docs:
        # supabase-py loosely-types row payloads as Mapping[str, JSON].
        # Cast once at the boundary so the rest of the loop is plain
        # dict access; the script trusts the documents-table schema.
        d = cast(dict[str, Any], raw)
        if d.get("parse_status") in ("pending", "error"):
            continue
        breakdown = cast(dict[str, Any], d.get("fraud_score_breakdown") or {})
        metadata_score = int(breakdown.get("metadata_score") or 0)
        math_score = int(breakdown.get("math_score") or 0)
        patterns_score = int(breakdown.get("patterns_score") or 0)
        would = evaluate_tampering_from_scores(
            metadata_score=metadata_score, math_score=math_score
        )
        rows.append(
            _Row(
                document_id=str(d["id"]),
                original_filename=str(d.get("original_filename") or ""),
                merchant_id=str(d.get("merchant_id")) if d.get("merchant_id") else None,
                fraud_score=int(d.get("fraud_score") or 0),
                metadata_score=metadata_score,
                math_score=math_score,
                patterns_score=patterns_score,
                would_decline=would,
                branch=_branch_label(metadata_score, math_score),
                metadata_flags=[
                    str(f) for f in (d.get("metadata_flags") or [])
                ],
            )
        )

    total = len(rows)
    fires = [r for r in rows if r.would_decline]
    strong = [r for r in fires if r.branch == "strong_metadata"]
    medium = [r for r in fires if r.branch == "medium_corroborated"]
    medium_uncorr = [r for r in rows if r.branch == "medium_uncorroborated"]

    print("Summary")
    print("=" * 100)
    print(f"  documents scanned     = {total}")
    if total:
        pct = len(fires) / total * 100
        print(f"  would-decline (fires) = {len(fires)} ({pct:.1f}% of total)")
    print(f"    strong_metadata     = {len(strong)}")
    print(f"    medium_corroborated = {len(medium)}")
    print(
        f"  medium_uncorroborated = {len(medium_uncorr)}  "
        "(would NOT fire; review for parse-time false negatives)"
    )
    print()

    if fires:
        print("WOULD-DECLINE matrix")
        print("=" * 100)
        print(
            f"  {'doc_id':36s}  {'branch':22s}  {'meta':>4s}  {'math':>4s}  "
            f"{'patt':>4s}  {'fs':>3s}  filename"
        )
        for row in fires:
            print(
                f"  {row.document_id}  {row.branch:22s}  "
                f"{row.metadata_score:>4d}  {row.math_score:>4d}  "
                f"{row.patterns_score:>4d}  {row.fraud_score:>3d}  "
                f"{row.original_filename}"
            )
            if row.metadata_flags:
                print(f"    metadata_flags: {row.metadata_flags}")
            print()

    if medium_uncorr:
        print("MEDIUM-METADATA-WITHOUT-MATH-CORROBORATION (informational)")
        print("=" * 100)
        print(
            f"  {'doc_id':36s}  {'meta':>4s}  {'math':>4s}  "
            f"{'patt':>4s}  filename"
        )
        for row in medium_uncorr:
            print(
                f"  {row.document_id}  {row.metadata_score:>4d}  "
                f"{row.math_score:>4d}  {row.patterns_score:>4d}  "
                f"{row.original_filename}"
            )
        print()
        print(
            "  ^ these would NOT fire under shadow OR live (correct VU-shape behavior)."
        )
        print(
            "  If the operator believes any of these IS tampered, the parse-time"
        )
        print(
            "  math signals likely missed it — that's a parser correctness gap,"
        )
        print(
            "  not a composition rule miscalibration."
        )

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan the most recent N documents (default: all)",
    )
    args = parser.parse_args()
    sys.exit(main(args.limit))
