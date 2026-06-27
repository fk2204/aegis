"""Stripe document router — CSV vs. PDF vision dispatch.

A Stripe statement reaches AEGIS through one of two paths:

* CSV — Dashboard → Reports → Balance transactions → Download. Pure
  text, no Bedrock, no vision pass. Handled by
  ``aegis.parser.processor.csv_stripe.extract_stripe_csv``.
* PDF — Dashboard → Payouts → Export. Image-rich; goes through the
  existing ``extract_stripe`` Bedrock path (vision-capable extractor).

This module is the single entry point the upload route uses for
Stripe. It picks the path based on filename extension + signature
detection, runs the appropriate extractor, validates, aggregates,
and returns a ``StripeParseResult`` with the dossier-shape
aggregates plus a ``parse_method`` discriminator.

The bank pipeline orchestration (``aegis.parser.pipeline.run_pipeline``)
is NOT involved here — processor statements have their own
validation gate, their own aggregates, and don't compute fraud_score
the same way bank statements do. The worker calls this directly via
``aegis.parser.processor.pipeline.run_processor_pipeline`` for the
PDF path; the CSV path doesn't have a worker hook yet (deferred
follow-up; not in the scope of this commit).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from aegis.llm import LLMClient
from aegis.parser.processor.aggregate import aggregate_processor
from aegis.parser.processor.csv_square import SquareCsvError, extract_square_csv
from aegis.parser.processor.csv_stripe import StripeCsvError, extract_stripe_csv
from aegis.parser.processor.detect import (
    ProcessorBrand,
    detect_processor,
    detect_processor_from_filename,
)
from aegis.parser.processor.dossier_aggregates import (
    StripeParseResult,
    build_stripe_dossier_aggregates,
)
from aegis.parser.processor.extract_stripe import (
    ProcessorExtractionError,
    extract_stripe,
)
from aegis.parser.processor.validate import validate_processor


class StripeRouterError(RuntimeError):
    """Raised when Stripe routing can't pick a path or extraction failed."""


def route_stripe_document(
    *,
    filename: str | Path,
    file_bytes: bytes,
    llm: LLMClient,
    business_name: str | None = None,
) -> StripeParseResult:
    """Route a Stripe document to the correct extractor.

    Parameters
    ----------
    filename
        Original filename. Used for extension + token detection. Path
        components are ignored.
    file_bytes
        Raw file content. Caller is responsible for reading from disk
        and applying the application-level size limit.
    llm
        Injected ``LLMClient``. Required for the PDF path; unused on
        CSV (which is fully deterministic).
    business_name
        Optional pass-through for the CSV summary (Stripe CSVs don't
        carry the business name — the operator may have it from the
        merchant row).

    Returns
    -------
    StripeParseResult
        Validated extraction + dossier-shape aggregates +
        ``parse_method`` discriminator.

    Raises
    ------
    StripeRouterError
        On unrecognized extension or upstream extraction failure
        (chained for context). The CSV path validation gate also
        raises here when the math identity doesn't hold — the caller
        treats both as ``manual_review``.
    """
    suffix = Path(str(filename)).suffix.lower()

    if suffix == ".csv":
        try:
            extraction = extract_stripe_csv(file_bytes, business_name=business_name)
        except StripeCsvError as exc:
            raise StripeRouterError(f"CSV extraction failed: {exc}") from exc
        parse_method: Literal["csv", "pdf_vision"] = "csv"
    elif suffix == ".pdf":
        try:
            extraction = extract_stripe(file_bytes, llm)
        except ProcessorExtractionError as exc:
            raise StripeRouterError(f"PDF extraction failed: {exc}") from exc
        parse_method = "pdf_vision"
    else:
        raise StripeRouterError(
            f"unsupported Stripe document extension: {suffix!r} "
            f"(filename={Path(str(filename)).name!r}); expected .csv or .pdf"
        )

    # Same validation gate the PDF processor pipeline uses. CSV paths
    # have their summary built from summed rows, so the per-kind tie-out
    # is trivially true; the period sanity + source attribution checks
    # still run and remain load-bearing.
    validation = validate_processor(extraction)
    if not validation.passed:
        raise StripeRouterError(
            "Stripe statement failed validation: " + "; ".join(validation.failures)
        )

    base_aggregates = aggregate_processor(extraction.transactions)
    dossier_aggregates = build_stripe_dossier_aggregates(extraction, base_aggregates)

    return StripeParseResult(
        extraction=extraction,
        aggregates=dossier_aggregates,
        parse_method=parse_method,
        period_days=dossier_aggregates.period_days,
    )


def route_square_document(
    *,
    filename: str | Path,
    file_bytes: bytes,
    business_name: str | None = None,
) -> StripeParseResult:
    """Route a Square CSV document to the deterministic extractor.

    Mirrors ``route_stripe_document`` for Square's CSV path. Square
    PDFs go through the existing ``extract_square`` Bedrock-vision
    extractor via ``run_processor_pipeline`` (worker hook); this CSV
    path is the deterministic alternative the upload route picks when
    the file extension is ``.csv``.

    Parameters
    ----------
    filename
        Original filename. Used only to verify the extension is
        ``.csv`` — content discrimination happened upstream in the
        upload route via ``detect_processor_from_csv_header``.
    file_bytes
        Raw CSV bytes.
    business_name
        Optional pass-through for the Square summary (Square CSVs
        don't carry the business name).

    Returns
    -------
    StripeParseResult
        Validated extraction + dossier-shape aggregates +
        ``parse_method="csv"``. The shape is the same as the Stripe
        CSV path — the dossier template only cares about the
        aggregate surface, not the brand-specific underpinnings.

    Raises
    ------
    StripeRouterError
        On unsupported extension or upstream extraction failure (the
        ``SquareCsvError`` is chained for context).
    """
    suffix = Path(str(filename)).suffix.lower()
    if suffix != ".csv":
        raise StripeRouterError(
            f"unsupported Square document extension: {suffix!r} "
            f"(filename={Path(str(filename)).name!r}); expected .csv"
        )
    try:
        extraction = extract_square_csv(file_bytes, business_name=business_name)
    except SquareCsvError as exc:
        raise StripeRouterError(f"Square CSV extraction failed: {exc}") from exc

    validation = validate_processor(extraction)
    if not validation.passed:
        raise StripeRouterError(
            "Square statement failed validation: " + "; ".join(validation.failures)
        )

    base_aggregates = aggregate_processor(extraction.transactions)
    dossier_aggregates = build_stripe_dossier_aggregates(extraction, base_aggregates)

    return StripeParseResult(
        extraction=extraction,
        aggregates=dossier_aggregates,
        parse_method="csv",
        period_days=dossier_aggregates.period_days,
    )


def detect_stripe(
    *,
    filename: str | Path,
    pdf_path: str | Path | None = None,
) -> ProcessorBrand:
    """Return the brand a Stripe document advertises.

    Two-step routing:
      1. Filename hit (cheap, pure string) — short-circuits if it
         matches Stripe / Square / ambiguous tokens.
      2. PDF content sniff (only when filename is silent AND a
         ``pdf_path`` is provided) — uses the existing
         ``detect_processor`` content detector.

    CSV paths whose filename doesn't carry a Stripe token return
    ``"bank"`` — the caller should refuse the upload (we don't know
    how to parse an untagged CSV).
    """
    filename_brand = detect_processor_from_filename(filename)
    if filename_brand != "bank":
        return filename_brand
    if pdf_path is not None:
        return detect_processor(pdf_path).brand
    return "bank"


__all__ = [
    "StripeRouterError",
    "detect_stripe",
    "route_square_document",
    "route_stripe_document",
]


# ---------------------------------------------------------------------------
# Bridge into ``aegis.parser.pipeline`` — public ``processor_type`` accessor.
# ---------------------------------------------------------------------------


def processor_type_for_document(
    *,
    filename: str | Path,
    pdf_path: str | Path | None = None,
) -> str | None:
    """Return ``"stripe"`` / ``"square"`` / None for the document.

    Public stable accessor the bank-pipeline orchestrator
    (``aegis.parser.pipeline``) exposes via its
    ``determine_processor_type`` re-export. None means "not a
    processor statement — route to the bank pipeline."

    "stripe" filename-or-content match → "stripe".
    "square" filename-or-content match → "square".
    "ambiguous" → None (the caller fails closed via the worker's
    ambiguous-processor handling; we don't pretend we resolved a brand
    we couldn't disambiguate).
    "bank" → None.
    """
    brand = detect_stripe(filename=filename, pdf_path=pdf_path)
    if brand in ("stripe", "square"):
        return brand
    return None
