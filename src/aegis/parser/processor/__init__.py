"""Payment-processor statement parser (mp Phase 6.6 / Stage 2C).

Sibling to ``aegis.parser`` (which handles bank statements). Processor
statements (Stripe, Square) carry a different set of aggregates —
gross, refunds, chargebacks, fees, payouts — and a different
validation gate (the reconciliation is gross - refunds - chargebacks
- fees == payouts +/- $0.01 rather than the bank-statement daily
running-balance tie-out).

Two-pass + deterministic validation discipline is preserved exactly
as in the bank parser:
    1. detect      — which processor (signature inspection, no LLM)
    2. extract     — LLM pass 1: pull line items + printed totals
    3. validate    — deterministic gate: math reconciles
    4. aggregate   — deterministic metrics with full source attribution
"""

from __future__ import annotations

from aegis.parser.processor.aggregate import ProcessorAggregates, aggregate_processor
from aegis.parser.processor.detect import (
    ProcessorBrand,
    ProcessorDetection,
    detect_processor,
)
from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
    ProcessorLineKind,
    ProcessorSummary,
)
from aegis.parser.processor.pipeline import ProcessorPipelineResult, run_processor_pipeline
from aegis.parser.processor.validate import ProcessorValidationResult, validate_processor

__all__ = [
    "ExtractedProcessorStatement",
    "ProcessorAggregates",
    "ProcessorBrand",
    "ProcessorDetection",
    "ProcessorLineItem",
    "ProcessorLineKind",
    "ProcessorPipelineResult",
    "ProcessorSummary",
    "ProcessorValidationResult",
    "aggregate_processor",
    "detect_processor",
    "run_processor_pipeline",
    "validate_processor",
]
