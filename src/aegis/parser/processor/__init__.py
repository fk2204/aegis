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
from aegis.parser.processor.csv_clover import CloverCsvError, extract_clover_csv
from aegis.parser.processor.csv_square import SquareCsvError, extract_square_csv
from aegis.parser.processor.csv_stripe import StripeCsvError, extract_stripe_csv
from aegis.parser.processor.csv_toast import ToastCsvError, extract_toast_csv
from aegis.parser.processor.detect import (
    ProcessorBrand,
    ProcessorDetection,
    detect_processor,
    detect_processor_from_csv_header,
    detect_processor_from_filename,
)
from aegis.parser.processor.dossier_aggregates import (
    ParseMethod,
    StripeDossierAggregates,
    StripeParseResult,
    build_stripe_dossier_aggregates,
)
from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
    ProcessorLineKind,
    ProcessorSummary,
)
from aegis.parser.processor.pipeline import ProcessorPipelineResult, run_processor_pipeline
from aegis.parser.processor.repository import (
    InMemoryProcessorStatementRepository,
    ProcessorStatementNotFoundError,
    ProcessorStatementRepository,
    ProcessorStatementRow,
    ProcessorStatementWriteError,
    ProcessorType,
    SupabaseProcessorStatementRepository,
)
from aegis.parser.processor.stripe_router import (
    StripeRouterError,
    detect_stripe,
    processor_type_for_document,
    route_square_document,
    route_stripe_document,
)
from aegis.parser.processor.validate import ProcessorValidationResult, validate_processor

__all__ = [
    "CloverCsvError",
    "ExtractedProcessorStatement",
    "InMemoryProcessorStatementRepository",
    "ParseMethod",
    "ProcessorAggregates",
    "ProcessorBrand",
    "ProcessorDetection",
    "ProcessorLineItem",
    "ProcessorLineKind",
    "ProcessorPipelineResult",
    "ProcessorStatementNotFoundError",
    "ProcessorStatementRepository",
    "ProcessorStatementRow",
    "ProcessorStatementWriteError",
    "ProcessorSummary",
    "ProcessorType",
    "ProcessorValidationResult",
    "SquareCsvError",
    "StripeCsvError",
    "StripeDossierAggregates",
    "StripeParseResult",
    "StripeRouterError",
    "SupabaseProcessorStatementRepository",
    "ToastCsvError",
    "aggregate_processor",
    "build_stripe_dossier_aggregates",
    "detect_processor",
    "detect_processor_from_csv_header",
    "detect_processor_from_filename",
    "detect_stripe",
    "extract_clover_csv",
    "extract_square_csv",
    "extract_stripe_csv",
    "extract_toast_csv",
    "processor_type_for_document",
    "route_square_document",
    "route_stripe_document",
    "run_processor_pipeline",
    "validate_processor",
]
