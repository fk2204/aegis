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
- `metadata.eof_markers > EOF_HARD_DECLINE` (incremental save spam)
- `metadata.fraud_score >= 60`
- `weighted fraud_score >= HARD_DECLINE_THRESHOLD`
- compound-signal floor (two moderate signals together) elevates score
  to at least HARD_DECLINE_THRESHOLD

EOF threshold
-------------
Originally `eof_markers > 1` auto-rejected any PDF with 2+ `%%EOF`
markers. Real-world testing on operator-supplied bank statements (Nov
2025 - May 2026, 3 banks) surfaced this as a false-positive factory:
legitimate online-banking exports routinely have 2 EOFs (the bank's
export tool writes one, the user's PDF viewer or browser re-saves and
appends another). The bar is now `EOF_HARD_DECLINE = 2`, so 3+ EOFs
still hard-fail (genuine incremental-save tampering) but 2 EOFs is
demoted to a `review` flag the operator can clear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from aegis.config import get_settings
from aegis.llm import LLMClient
from aegis.logger import get_logger
from aegis.parser.aggregate import aggregate
from aegis.parser.classify import (
    avg_classification_confidence,
    classify_transactions,
    per_category_confidence,
)
from aegis.parser.extract import (
    ExtractionPass1Result,
    extract_statement,
    extract_statement_per_page,
    extract_statement_via_vision,
)
from aegis.parser.metadata import MetadataAnalysis, analyze_metadata
from aegis.parser.models import Aggregates, ClassifiedTransaction, ValidationResult
from aegis.parser.nsf_secondary import secondary_validate_nsf
from aegis.parser.page_router import (
    PageStrategyDecision,
    classify_pages,
    has_low_confidence,
    is_homogeneous,
    summarize,
)
from aegis.parser.patterns import PatternAnalysis, analyze_patterns
from aegis.parser.tampering import TamperingEvaluation, evaluate_tampering
from aegis.parser.validate import validate_extraction

_log = get_logger(__name__)

# THE thresholds. Read from here, not from anywhere else. (TS had three.)
FRAUD_WEIGHTS: Final[dict[str, float]] = {
    "metadata": 0.35,
    "math": 0.40,
    "patterns": 0.25,
}
HARD_DECLINE_THRESHOLD: Final[int] = 65
REVIEW_THRESHOLD: Final[int] = 35
METADATA_HARD_DECLINE: Final[int] = 60
# EOF marker count above which a PDF is treated as genuinely tampered.
# 2 EOFs are normal for legit online-banking exports (bank writes one,
# viewer/browser re-save appends another). 3+ indicates real incremental
# save tampering.
EOF_HARD_DECLINE: Final[int] = 2

# Maximum page count for the OCR vision fallback. Vision tokens are
# ~5-8x text-extraction tokens; capping the page count bounds the
# Bedrock cost on accidentally-uploaded huge scans. Statements above
# this cap that lack a text layer land in manual_review with reason
# `ocr_oversize_image_pdf` so the operator can split or rescan them.
MAX_OCR_PAGES: Final[int] = 20

# Average classification-confidence floor. Below this, the document goes
# to manual_review regardless of math / metadata / pattern scores —
# classifier signaling low confidence is the LLM telling us it can't
# read the rows accurately, and downstream patterns + aggregates are
# only as good as the labels.
#
# Tune based on real-deal data after ~50 funded deals. Track per-statement
# avg confidence distribution in operator dashboard to inform tuning.
CLASSIFICATION_CONFIDENCE_FLOOR: Final[int] = 60

# Per-category floor for high-impact categories (mca_debit drives the
# stacking pattern + scoring penalties; low confidence there poisons
# both). Same tuning guidance as above — adjust once we have signal
# from real funded deals.
HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR: Final[int] = 70
HIGH_IMPACT_CATEGORIES: Final[frozenset[str]] = frozenset({"mca_debit", "nsf_fee"})

# R1.7 — ADB partial-coverage escalation threshold (shadow). When the
# aggregator's adb_partial_coverage flag reports skipped/period > 10%,
# the average-daily-balance metric is computed over too few days to be
# trusted. Decision-boundary rule (CLAUDE.md) requires shadow-mode first
# for anything that would route a doc from proceed → manual_review, so
# this commit only EMITS a flag (`adb_coverage_thin:...would_route_review`)
# and does NOT change parse_status. Operator validates against corpus
# then flips via config in a follow-up commit.
ADB_COVERAGE_THIN_RATIO_THRESHOLD: Final[Decimal] = Decimal("0.10")

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
    # Average classification confidence across all classified rows
    # (100 when no rows were classified — e.g. validation failed). Used
    # by the parse_status gate and surfaced on the merchant detail page.
    avg_classification_confidence: int = 100
    # Per-category averages keyed by category name. Empty when no rows
    # were classified. Used for high-impact category gating.
    classification_confidence_by_category: dict[str, int] = field(default_factory=dict)
    # Per-calendar-month roll-up (deposits, withdrawals, avg_balance).
    # Persisted on analyses.monthly_breakdown for renewal-merchant
    # month-over-month deltas. Decimals stored as strings for jsonb.
    monthly_breakdown: list[dict[str, str]] = field(default_factory=list)
    # Tampering composition evaluation — see
    # ``aegis.parser.tampering.evaluate_tampering``. None for pipeline
    # paths that short-circuit before metadata + math signals are
    # available (e.g. page-router low-confidence early exit). The
    # worker writes the shadow / live audit row when this is set and
    # ``tampering_evaluation.fires`` is True.
    tampering_evaluation: TamperingEvaluation | None = None


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

    settings = get_settings()
    page_decisions: list[PageStrategyDecision] = []
    if settings.aegis_parser_page_routing:
        page_decisions = classify_pages(pdf_path)
        _log.info(
            "parser.page_router.decisions",
            extra={"document": str(pdf_path), **summarize(page_decisions)},
        )
        if page_decisions and has_low_confidence(page_decisions):
            # Per master plan §6.5: any page with both strategies below
            # the floor fails the whole doc closed. Partial extraction
            # would poison the aggregates.
            synthetic_validation = ValidationResult(
                passed=False,
                failures=["page_router_low_confidence"],
            )
            return PipelineResult(
                parse_status="manual_review",
                metadata=metadata,
                extraction=None,
                validation=synthetic_validation,
                all_flags=_collect_flags(metadata, synthetic_validation, None, []),
            )

    used_ocr_fallback = False
    used_per_page_routing = False
    if settings.aegis_parser_page_routing and page_decisions and not is_homogeneous(page_decisions):
        # Mixed strategies → per-page extraction. is_homogeneous None /
        # homogeneous → fall through to the legacy single-call path so
        # the simple cases keep the cheaper one-shot behavior.
        extraction = extract_statement_per_page(pdf_bytes, llm, page_decisions)
        used_per_page_routing = True
        used_ocr_fallback = any(d.strategy == "vision" for d in page_decisions)
    elif not metadata.has_text_layer:
        if metadata.page_count > MAX_OCR_PAGES:
            # Image-only PDF larger than the vision-cost cap. Refuse to
            # OCR; route to manual_review so the operator can split the
            # document or request a fresh export with a text layer.
            synthetic_validation = ValidationResult(
                passed=False,
                failures=[
                    f"ocr_oversize_image_pdf: page_count={metadata.page_count}"
                    f" max={MAX_OCR_PAGES}"
                ],
            )
            return PipelineResult(
                parse_status="manual_review",
                metadata=metadata,
                extraction=None,
                validation=synthetic_validation,
                all_flags=_collect_flags(metadata, synthetic_validation, None, []),
            )
        extraction = extract_statement_via_vision(pdf_bytes, llm)
        used_ocr_fallback = True
    else:
        extraction = extract_statement(pdf_bytes, llm)

    validation = validate_extraction(
        extraction.statement,
        truncated=extraction.truncated,
        today=today,
    )

    if not validation.passed:
        flags = _collect_flags(metadata, validation, None, [])
        if used_ocr_fallback:
            flags.append("[META] ocr_fallback_used")
        if used_per_page_routing:
            flags.append("[META] per_page_routing_used")
        return PipelineResult(
            parse_status="manual_review",
            metadata=metadata,
            extraction=extraction,
            validation=validation,
            all_flags=flags,
        )

    classified = classify_transactions(extraction.statement.transactions, llm)
    patterns = analyze_patterns(
        classified,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
        today=today,
    )
    aggregate_result = aggregate(
        classified,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
        beginning_balance=extraction.statement.summary.beginning_balance,
    )
    aggregates = aggregate_result.aggregates

    avg_conf = avg_classification_confidence(classified)
    per_cat_conf = per_category_confidence(classified)
    confidence_failures = _confidence_failures(avg_conf, per_cat_conf)

    triangulation_flag = _fraud_cluster_triangulation(patterns)

    math_score = _math_score(validation)
    patterns_score_with_bump = patterns.fraud_score
    if triangulation_flag is not None:
        # Triangulated cluster: multiple independent red flags fired
        # together. Bump the patterns score by 10 (capped 100) so the
        # combined fraud_score reflects the correlation. The triangulation
        # rule is intentionally simple — refine after real-deal signal.
        patterns_score_with_bump = min(100, patterns.fraud_score + 10)

    fraud_score, breakdown, compound_flags = _fraud_score(
        metadata.fraud_score, math_score, patterns_score_with_bump
    )

    # Tampering composition (operator policy 2026-06-04): runs on every
    # parse, attached to the result; the worker decides whether to gate
    # (live mode) or just audit (shadow mode). Pure function over the
    # already-computed scores + validation failures.
    tampering_eval = evaluate_tampering(
        metadata_score=metadata.fraud_score,
        math_score=math_score,
        validation_failures=list(validation.failures),
    )

    parse_status = _decide(metadata, fraud_score, validation, confidence_failures)

    all_flags = _collect_flags(metadata, validation, patterns, compound_flags)
    all_flags.extend(f"[AGGREGATE] {f}" for f in aggregate_result.flags)
    all_flags.extend(f"[CONFIDENCE] {f}" for f in confidence_failures)
    if triangulation_flag is not None:
        all_flags.append(f"[COMPOUND] {triangulation_flag}")
    if used_ocr_fallback:
        all_flags.append("[META] ocr_fallback_used")
    if used_per_page_routing:
        all_flags.append("[META] per_page_routing_used")

    # R1.7 — ADB partial-coverage escalation (SHADOW). When the aggregator
    # reports more than 10% of period days skipped, the ADB metric is
    # computed over too narrow a window to be trustworthy. Emit a shadow
    # flag that documents the would-be routing change without touching
    # parse_status — operator flips via config after corpus validation.
    adb_thin_flag = _adb_coverage_thin_flag(aggregate_result.flags)
    if adb_thin_flag is not None:
        all_flags.append(f"[SHADOW] {adb_thin_flag}")

    # R1.8 — NSF secondary validation (SHADOW). Re-checks every LLM-labeled
    # nsf_fee row against running-balance + co-located return / reversal /
    # chargeback evidence. Low-confidence NSF rows fire separately. Pure
    # evidence emission; the row's category and parse_status are unchanged.
    nsf_issues = secondary_validate_nsf(
        classified,
        beginning_balance=extraction.statement.summary.beginning_balance,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
    )
    all_flags.extend(f"[SHADOW] {issue.flag_text}" for issue in nsf_issues)

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
        avg_classification_confidence=avg_conf,
        classification_confidence_by_category=per_cat_conf,
        monthly_breakdown=aggregate_result.monthly_breakdown,
        tampering_evaluation=tampering_eval,
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
    confidence_failures: list[str],
) -> ParseStatus:
    if metadata.eof_markers > EOF_HARD_DECLINE:
        return "manual_review"
    if metadata.fraud_score >= METADATA_HARD_DECLINE:
        return "manual_review"
    if fraud_score >= HARD_DECLINE_THRESHOLD:
        return "manual_review"
    if confidence_failures:
        # LLM signaled it couldn't classify accurately — labels are
        # suspect and downstream patterns/aggregates inherit the noise.
        # Same severity as a math gate failure.
        return "manual_review"
    if (
        fraud_score >= REVIEW_THRESHOLD
        or not validation.passed
        or metadata.eof_markers > 1
    ):
        return "review"
    return "proceed"


def _fraud_cluster_triangulation(patterns: PatternAnalysis | None) -> str | None:
    """Three+ independent patterns with at least one severity >= 25 -> triangulated.

    Single patterns are routine; clusters of three are not. The flag is
    informational + a +10 bump on the patterns score so the combined
    fraud_score reflects the correlation between independent red flags.

    Tune based on real-deal data after ~50 funded deals.
    """
    if patterns is None or len(patterns.patterns) < 3:
        return None
    if not any(p.severity >= 25 for p in patterns.patterns):
        return None
    codes = [p.code for p in patterns.patterns]
    return (
        f"fraud_cluster_triangulated:{len(patterns.patterns)}_signals_"
        + ",".join(codes[:5])
    )


def _confidence_failures(
    avg_conf: int, per_cat_conf: dict[str, int]
) -> list[str]:
    """Return failure codes for classification confidence below the floor.

    Empty list = no failure. Two paths trigger:
      1. Overall avg below CLASSIFICATION_CONFIDENCE_FLOOR.
      2. Any high-impact category (mca_debit, nsf_fee) below
         HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR.
    """
    failures: list[str] = []
    if avg_conf < CLASSIFICATION_CONFIDENCE_FLOOR:
        failures.append(
            f"classification_confidence_below_floor: avg={avg_conf} "
            f"floor={CLASSIFICATION_CONFIDENCE_FLOOR}"
        )
    for cat in HIGH_IMPACT_CATEGORIES:
        cat_conf = per_cat_conf.get(cat)
        if cat_conf is None:
            continue
        if cat_conf < HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR:
            failures.append(
                f"classification_confidence_below_floor_{cat}: "
                f"avg={cat_conf} floor={HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR}"
            )
    return failures


def _adb_coverage_thin_flag(aggregate_flags: list[str]) -> str | None:
    """Parse the aggregator's `adb_partial_coverage:{skipped}/{period}` flag.

    Returns a shadow-flag string when ``skipped / period > 10%``, else None.
    The flag name explicitly carries ``would_route_review`` so operators
    can see what the live routing would do after the shadow-mode flip
    (decision-boundary rule from CLAUDE.md).

    Format of the source flag (set in ``parser/aggregate.py``):
        ``adb_partial_coverage:{int}/{int}``

    Returns None on:
      - absent flag (clean coverage, nothing to surface)
      - malformed numerator/denominator (defense against future format drift)
      - zero or negative denominator (math undefined)
      - ratio at or below the threshold (10% is allowed)
    """
    prefix = "adb_partial_coverage:"
    for flag in aggregate_flags:
        if not flag.startswith(prefix):
            continue
        payload = flag[len(prefix):]
        parts = payload.split("/", maxsplit=1)
        if len(parts) != 2:
            return None
        try:
            skipped = int(parts[0])
            period = int(parts[1])
        except ValueError:
            return None
        if period <= 0:
            return None
        if skipped <= 0:
            return None
        ratio = Decimal(skipped) / Decimal(period)
        if ratio <= ADB_COVERAGE_THIN_RATIO_THRESHOLD:
            return None
        # Quantize to whole-percent for stable rendering; threshold is 10%
        # so single-digit precision is sufficient.
        ratio_pct = int((ratio * Decimal("100")).to_integral_value())
        threshold_pct = int(
            (ADB_COVERAGE_THIN_RATIO_THRESHOLD * Decimal("100")).to_integral_value()
        )
        return (
            f"adb_coverage_thin:skip_ratio={ratio_pct}pct_"
            f"threshold={threshold_pct}pct_would_route_review"
        )
    return None


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
    "ADB_COVERAGE_THIN_RATIO_THRESHOLD",
    "CLASSIFICATION_CONFIDENCE_FLOOR",
    "EOF_HARD_DECLINE",
    "FRAUD_WEIGHTS",
    "HARD_DECLINE_THRESHOLD",
    "HIGH_IMPACT_CATEGORIES",
    "HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR",
    "MAX_OCR_PAGES",
    "METADATA_HARD_DECLINE",
    "REVIEW_THRESHOLD",
    "PipelineResult",
    "run_pipeline",
]
