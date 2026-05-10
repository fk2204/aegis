"""End-to-end parser pipeline orchestrator.

Order of operations
-------------------
1. metadata    -> tampering signals (deterministic, pikepdf)
2. extract     -> raw transactions + printed summary (pass 1, LLM)
3. validate    -> deterministic gate (period tie-out + daily reconciliation
                  + source attribution). FAIL -> manual_review, no retry.
4. classify    -> per-transaction category + confidence (pass 2, LLM)
5. patterns    -> deterministic fraud detectors over classified rows
6. aggregate   -> deterministic metrics with full source attribution

Fraud-score weights are centralized as ONE constant (`FRAUD_WEIGHTS`).
TS had three threshold constants in different files that drifted; fix is
to read all thresholds from this module.

Hard-decline triggers
---------------------
- `metadata.eof_markers > 1` (incremental save)
- `metadata.fraud_score >= 60`
- `weighted fraud_score >= HARD_DECLINE_THRESHOLD`
- compound-signal floor (two moderate signals together) elevates score
  to at least HARD_DECLINE_THRESHOLD
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Final, Literal

from aegis.llm import LLMClient
from aegis.parser.aggregate import aggregate
from aegis.parser.classify import classify_transactions
from aegis.parser.extract import ExtractionPass1Result, extract_statement
from aegis.parser.metadata import MetadataAnalysis, analyze_metadata
from aegis.parser.models import Aggregates, ClassifiedTransaction, ValidationResult
from aegis.parser.patterns import PatternAnalysis, analyze_patterns
from aegis.parser.validate import validate_extraction

# THE thresholds. Read from here, not from anywhere else. (TS had three.)
FRAUD_WEIGHTS: Final[dict[str, float]] = {
    "metadata": 0.35,
    "math": 0.40,
    "patterns": 0.25,
}
HARD_DECLINE_THRESHOLD: Final[int] = 65
REVIEW_THRESHOLD: Final[int] = 35
METADATA_HARD_DECLINE: Final[int] = 60

ParseStatus = Literal["proceed", "review", "manual_review"]


@dataclass
class PipelineResult:
    parse_status: ParseStatus
    metadata: MetadataAnalysis
    extraction: ExtractionPass1Result | None
    validation: ValidationResult
    classified: list[ClassifiedTransaction] = field(default_factory=list)
    patterns: PatternAnalysis | None = None
    aggregates: Aggregates | None = None
    fraud_score: int = 0
    fraud_score_breakdown: dict[str, int] = field(default_factory=dict)
    all_flags: list[str] = field(default_factory=list)


def run_pipeline(
    pdf_path: str,
    llm: LLMClient,
    *,
    today: date | None = None,
) -> PipelineResult:
    """Run the full parser pipeline.

    Phase 5 will wrap this in an arq worker; for now it's synchronous.
    LLM is injected so tests can pass a fake.
    """
    metadata = analyze_metadata(pdf_path)

    pdf_bytes = _read_pdf(pdf_path)
    extraction = extract_statement(pdf_bytes, llm)

    validation = validate_extraction(
        extraction.statement,
        truncated=extraction.truncated,
        today=today,
    )

    if not validation.passed:
        return PipelineResult(
            parse_status="manual_review",
            metadata=metadata,
            extraction=extraction,
            validation=validation,
            all_flags=_collect_flags(metadata, validation, None, []),
        )

    classified = classify_transactions(extraction.statement.transactions, llm)
    patterns = analyze_patterns(
        classified,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
        today=today,
    )
    aggregates = aggregate(
        classified,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
        beginning_balance=extraction.statement.summary.beginning_balance,
    )

    math_score = _math_score(validation)
    fraud_score, breakdown, compound_flags = _fraud_score(
        metadata.fraud_score, math_score, patterns.fraud_score
    )

    parse_status = _decide(metadata, fraud_score, validation)

    all_flags = _collect_flags(metadata, validation, patterns, compound_flags)

    return PipelineResult(
        parse_status=parse_status,
        metadata=metadata,
        extraction=extraction,
        validation=validation,
        classified=classified,
        patterns=patterns,
        aggregates=aggregates,
        fraud_score=fraud_score,
        fraud_score_breakdown=breakdown,
        all_flags=all_flags,
    )


def _read_pdf(pdf_path: str) -> bytes:
    from pathlib import Path

    return Path(pdf_path).read_bytes()


def _math_score(validation: ValidationResult) -> int:
    """Same severity grading as TS: critical failures count more.

    Critical = reconciliation_failed_*, future_dated, extraction_truncated.
    """
    if not validation.failures:
        return 0
    critical_prefixes = ("reconciliation_failed", "future_dated", "extraction_truncated")
    critical = sum(
        1 for f in validation.failures if f.startswith(critical_prefixes)
    )
    n = len(validation.failures)
    if n == 1:
        return 55 if critical else 25
    if n == 2:
        return 85 if critical else 65
    return 100


def _fraud_score(
    metadata_score: int, math_score: int, patterns_score: int
) -> tuple[int, dict[str, int], list[str]]:
    raw = round(
        metadata_score * FRAUD_WEIGHTS["metadata"]
        + math_score * FRAUD_WEIGHTS["math"]
        + patterns_score * FRAUD_WEIGHTS["patterns"]
    )

    escalated = raw
    compound: list[str] = []
    if metadata_score >= 50 and patterns_score >= 40:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD)
        compound.append("metadata+patterns elevated together")
    if math_score >= 55 and patterns_score >= 40:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD + 5)
        compound.append("math_failure+patterns elevated together")
    if metadata_score >= 40 and math_score >= 40 and patterns_score >= 30:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD + 5)
        compound.append("three-layer signal convergence")
    if patterns_score >= 80:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD + 5)

    return (
        escalated,
        {
            "metadata_score": metadata_score,
            "math_score": math_score,
            "patterns_score": patterns_score,
        },
        compound,
    )


def _decide(
    metadata: MetadataAnalysis,
    fraud_score: int,
    validation: ValidationResult,
) -> ParseStatus:
    if metadata.eof_markers > 1:
        return "manual_review"
    if metadata.fraud_score >= METADATA_HARD_DECLINE:
        return "manual_review"
    if fraud_score >= HARD_DECLINE_THRESHOLD:
        return "manual_review"
    if fraud_score >= REVIEW_THRESHOLD or not validation.passed:
        return "review"
    return "proceed"


def _collect_flags(
    metadata: MetadataAnalysis,
    validation: ValidationResult,
    patterns: PatternAnalysis | None,
    compound: list[str],
) -> list[str]:
    out: list[str] = []
    out.extend(f"[META] {f}" for f in metadata.flags)
    out.extend(f"[MATH] {f}" for f in validation.failures)
    out.extend(f"[WARN] {f}" for f in validation.warnings)
    if patterns:
        out.extend(f"[PATTERN] {p.code}: {p.detail}" for p in patterns.patterns)
    out.extend(f"[COMPOUND] {f}" for f in compound)
    return out


__all__ = [
    "FRAUD_WEIGHTS",
    "HARD_DECLINE_THRESHOLD",
    "METADATA_HARD_DECLINE",
    "REVIEW_THRESHOLD",
    "PipelineResult",
    "run_pipeline",
]
