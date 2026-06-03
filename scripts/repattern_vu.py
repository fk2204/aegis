"""Re-run pattern analysis + recompute fraud_score for one merchant's
existing analyses. Companion to ``rescore_vu.py`` for the H10
(unreconciled_internal_transfer single-account severity) fix.

Reads each doc's classified transactions, re-invokes
``analyze_patterns()``, applies the triangulation bump and the
weighted-sum fraud_score formula (same as ``parser.pipeline``), prints a
per-doc before/after diff for the fraud-related fields, **pauses for
explicit YES**, then UPDATEs the analyses.pattern_analysis JSONB and the
documents.fraud_score + documents.fraud_score_breakdown columns.

Uses the existing persisted ``metadata_score`` and ``math_score`` from
``documents.fraud_score_breakdown`` — those are extraction- and
validation-side signals that don't change without re-parsing the PDF.

Usage on prod box:

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    uv run python scripts/repattern_vu.py <merchant_id> <actor_email>
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from supabase import create_client

from aegis.api.deps import get_audit, get_repository
from aegis.config import get_settings
from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import (
    PatternAnalysis,
    analyze_patterns,
    pattern_analysis_to_dto,
)
from aegis.parser.pipeline import (
    _fraud_cluster_triangulation,
    _fraud_score,
)


@dataclass
class _RepatternEntry:
    document_id: UUID
    filename: str
    before_fraud_score: int
    after_fraud_score: int
    before_patterns_score: int
    after_patterns_score: int
    metadata_score: int
    math_score: int
    before_pattern_codes: list[str]
    after_pattern_codes: list[str]
    new_pattern_analysis_dto: dict[str, object]
    new_fraud_score_breakdown: dict[str, int]


def _recompute(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
    metadata_score: int,
    math_score: int,
) -> tuple[PatternAnalysis, int, int, dict[str, int]]:
    patterns = analyze_patterns(transactions, period_start, period_end)
    triangulation_flag = _fraud_cluster_triangulation(patterns)
    patterns_score_with_bump = patterns.fraud_score
    if triangulation_flag is not None:
        patterns_score_with_bump = min(100, patterns.fraud_score + 10)
    fraud_score, breakdown, _ = _fraud_score(
        metadata_score, math_score, patterns_score_with_bump
    )
    return patterns, fraud_score, patterns_score_with_bump, breakdown


def main(merchant_id_str: str, actor_email: str) -> int:
    merchant_id = UUID(merchant_id_str)
    settings = get_settings()
    if settings.supabase_service_key is None:
        print("SUPABASE_SERVICE_KEY is not configured.", file=sys.stderr)
        return 2
    sb = create_client(
        settings.supabase_url, settings.supabase_service_key.get_secret_value()
    )
    repo = get_repository()
    audit = get_audit()

    docs = repo.list_documents(merchant_id=merchant_id, limit=50)
    if not docs:
        print(f"No documents for merchant {merchant_id}.")
        return 1

    print(f"Merchant: {merchant_id}")
    print(f"Documents: {len(docs)}")
    print()

    plan: list[_RepatternEntry] = []
    for d in sorted(docs, key=lambda x: x.original_filename):
        analysis = repo.get_analysis(d.id)
        if analysis is None:
            print(f"  [skip] {d.original_filename}: no analysis row")
            continue
        txns = repo.list_transactions(d.id)
        if not txns:
            print(f"  [skip] {d.original_filename}: no transactions")
            continue

        breakdown_before = d.fraud_score_breakdown or {}
        metadata_score = int(breakdown_before.get("metadata_score", 0))
        math_score = int(breakdown_before.get("math_score", 0))

        new_patterns, new_fraud_score, new_patterns_score, new_breakdown = _recompute(
            txns,
            analysis.statement_period_start,
            analysis.statement_period_end,
            metadata_score,
            math_score,
        )

        before_pattern_codes = []
        if analysis.pattern_analysis is not None:
            before_pattern_codes = [
                p.code for p in analysis.pattern_analysis.patterns
            ]

        after_pattern_codes = [p.code for p in new_patterns.patterns]

        new_dto = pattern_analysis_to_dto(new_patterns).model_dump(
            mode="json"
        )

        plan.append(
            _RepatternEntry(
                document_id=d.id,
                filename=d.original_filename,
                before_fraud_score=d.fraud_score or 0,
                after_fraud_score=new_fraud_score,
                before_patterns_score=int(
                    breakdown_before.get("patterns_score", 0)
                ),
                after_patterns_score=new_patterns_score,
                metadata_score=metadata_score,
                math_score=math_score,
                before_pattern_codes=before_pattern_codes,
                after_pattern_codes=after_pattern_codes,
                new_pattern_analysis_dto=new_dto,
                new_fraud_score_breakdown=new_breakdown,
            )
        )

    if not plan:
        print("Nothing to repattern.")
        return 0

    print("Repattern diff (per document)")
    print("=" * 100)
    for p in plan:
        print(f"  {p.filename}")
        print(f"    document_id        = {p.document_id}")
        print(
            f"    fraud_score        : {p.before_fraud_score:>3} -> "
            f"{p.after_fraud_score:>3}"
        )
        print(
            f"    patterns_score     : {p.before_patterns_score:>3} -> "
            f"{p.after_patterns_score:>3}"
        )
        print(
            f"    metadata_score     = {p.metadata_score}   "
            f"math_score = {p.math_score}"
        )
        gone = set(p.before_pattern_codes) - set(p.after_pattern_codes)
        added = set(p.after_pattern_codes) - set(p.before_pattern_codes)
        kept = set(p.before_pattern_codes) & set(p.after_pattern_codes)
        print(f"    patterns before    : {p.before_pattern_codes}")
        print(f"    patterns after     : {p.after_pattern_codes}")
        if gone:
            print(f"      - removed: {sorted(gone)}")
        if added:
            print(f"      + added:   {sorted(added)}")
        if kept and (gone or added):
            print(f"      = kept:    {sorted(kept)}")
        print()

    answer = input("Type YES (exact) to write these updates to prod: ")
    if answer.strip() != "YES":
        print("Aborted — no writes performed.")
        return 2

    ts = datetime.now(UTC).isoformat()
    written = 0
    for p in plan:
        # Analyses table: update pattern_analysis (jsonb). supabase-py
        # typing wants Mapping[str, JSON] — the dump-mode-json dict is
        # already JSON-safe at runtime.
        sb.table("analyses").update(
            {"pattern_analysis": p.new_pattern_analysis_dto}  # type: ignore[dict-item]
        ).eq("document_id", str(p.document_id)).execute()

        # Documents table: update fraud_score + fraud_score_breakdown
        sb.table("documents").update(
            {
                "fraud_score": p.after_fraud_score,
                "fraud_score_breakdown": p.new_fraud_score_breakdown,
            }
        ).eq("id", str(p.document_id)).execute()

        audit.record(
            actor="repattern_vu",
            actor_email=actor_email,
            action="analysis.repatterned",
            subject_type="document",
            subject_id=p.document_id,
            details={
                "merchant_id": str(merchant_id),
                "reason": "h10_unreconciled_severity_fix",
                "before_fraud_score": p.before_fraud_score,
                "after_fraud_score": p.after_fraud_score,
                "before_patterns_score": p.before_patterns_score,
                "after_patterns_score": p.after_patterns_score,
                "removed_pattern_codes": sorted(
                    set(p.before_pattern_codes) - set(p.after_pattern_codes)
                ),
                "added_pattern_codes": sorted(
                    set(p.after_pattern_codes) - set(p.before_pattern_codes)
                ),
                "repatterned_at": ts,
            },
        )
        written += 1
        print(f"  wrote {p.filename}")

    print(f"\nDone — {written} docs repatterned + fraud_score recomputed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "usage: repattern_vu.py <merchant_id> <actor_email>",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
