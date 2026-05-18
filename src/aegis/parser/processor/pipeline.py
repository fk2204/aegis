"""End-to-end processor-statement pipeline (mp Phase 6.6 / Stage 2C).

Order of operations:
    1. detect    — Stripe / Square (deterministic, pure)
    2. extract   — LLM pass 1: raw line items + printed summary
    3. validate  — deterministic gate (gross-refunds-chargebacks-fees
                   == payouts +/- $0.01, period sanity, per-kind tie-out)
    4. aggregate — deterministic metrics with source attribution

Mirrors ``aegis.parser.pipeline.run_pipeline`` for the bank flow.
The caller (upload route) inspects ``detect_processor`` first and
only invokes this orchestrator when the brand is "stripe" or
"square". When the brand is "ambiguous", the upload route fails
closed; when the brand is "bank", the bank pipeline runs instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aegis.llm import LLMClient
from aegis.logger import get_logger
from aegis.parser.processor.aggregate import ProcessorAggregates, aggregate_processor
from aegis.parser.processor.detect import ProcessorBrand
from aegis.parser.processor.extract_square import extract_square
from aegis.parser.processor.extract_stripe import (
    ProcessorExtractionError,
    extract_stripe,
)
from aegis.parser.processor.models import ExtractedProcessorStatement
from aegis.parser.processor.validate import (
    ProcessorValidationResult,
    validate_processor,
)

_log = get_logger(__name__)


ProcessorParseStatus = Literal["proceed", "review", "manual_review"]


# Chargeback ratio thresholds. >= 1% triggers a soft "review" flag —
# this is the industry watch-list level that processors themselves
# alert on. >= 2% is the typical "merchant on enforced reserve / risk
# of account suspension" level; we treat it as soft for now (operator
# can still approve) but with a more aggressive flag.
_CHARGEBACK_RATIO_SOFT = "0.01"
_CHARGEBACK_RATIO_HARD = "0.02"


@dataclass
class ProcessorPipelineResult:
    """Output of the processor parser run."""

    parse_status: ProcessorParseStatus
    brand: ProcessorBrand
    extraction: ExtractedProcessorStatement | None
    validation: ProcessorValidationResult
    aggregates: ProcessorAggregates | None = None
    flags: list[str] = field(default_factory=list)


def run_processor_pipeline(
    pdf_path: str | Path,
    pdf_bytes: bytes,
    llm: LLMClient,
    *,
    brand: ProcessorBrand,
) -> ProcessorPipelineResult:
    """Run the processor parser end-to-end.

    Parameters
    ----------
    pdf_path
        Path to the source PDF (used for logging only).
    pdf_bytes
        Raw PDF bytes — the caller is responsible for reading and
        size-checking the file. Mirrors ``run_pipeline``'s contract.
    llm
        Injected ``LLMClient`` (stubbed in tests).
    brand
        Result of ``detect_processor()``. Must be "stripe" or
        "square" — "bank" / "ambiguous" callers should NOT invoke
        this pipeline.

    Raises ``ProcessorExtractionError`` on a non-recoverable
    extraction failure (malformed JSON, schema rejection). A clean
    validation gate failure does NOT raise — it produces a result
    with ``parse_status="manual_review"`` so the doc surfaces in
    the dashboard with structured failure codes.
    """
    if brand not in ("stripe", "square"):
        raise ProcessorExtractionError(
            f"run_processor_pipeline called with brand={brand!r}; "
            f"caller must dispatch only stripe/square to this pipeline"
        )

    extract_fn = extract_stripe if brand == "stripe" else extract_square
    extraction = extract_fn(pdf_bytes, llm)

    validation = validate_processor(extraction)
    flags: list[str] = []
    flags.extend(f"[MATH] {f}" for f in validation.failures)
    flags.extend(f"[WARN] {f}" for f in validation.warnings)

    if not validation.passed:
        _log.info(
            "processor.pipeline.validation_failed",
            extra={
                "pdf_path": str(pdf_path),
                "brand": brand,
                "failure_count": len(validation.failures),
            },
        )
        return ProcessorPipelineResult(
            parse_status="manual_review",
            brand=brand,
            extraction=extraction,
            validation=validation,
            aggregates=None,
            flags=flags,
        )

    aggregates = aggregate_processor(extraction.transactions)

    parse_status: ProcessorParseStatus = "proceed"
    ratio = aggregates.chargeback_ratio
    if str(ratio) > _CHARGEBACK_RATIO_HARD:
        # >=2% chargebacks: industry watch-list territory. Operator
        # routes to review; downstream scoring penalizes.
        parse_status = "review"
        flags.append(
            f"[RISK] chargeback_ratio_high: {ratio:.4f} >= {_CHARGEBACK_RATIO_HARD}"
        )
    elif str(ratio) > _CHARGEBACK_RATIO_SOFT:
        # >=1% chargebacks: elevated but not catastrophic. Soft flag.
        parse_status = "review"
        flags.append(
            f"[RISK] chargeback_ratio_elevated: {ratio:.4f} >= {_CHARGEBACK_RATIO_SOFT}"
        )

    return ProcessorPipelineResult(
        parse_status=parse_status,
        brand=brand,
        extraction=extraction,
        validation=validation,
        aggregates=aggregates,
        flags=flags,
    )


__all__ = [
    "ProcessorParseStatus",
    "ProcessorPipelineResult",
    "run_processor_pipeline",
]
