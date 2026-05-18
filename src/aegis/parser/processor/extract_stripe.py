"""Stripe statement extraction (LLM pass 1).

Reads a Stripe statement PDF and returns ``ExtractedProcessorStatement``.
NO classification here. NO aggregates. The downstream
``aegis.parser.processor.validate.validate_processor`` decides whether
the document proceeds.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from aegis.llm import LLMClient
from aegis.parser.processor.models import ExtractedProcessorStatement

_MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024


STRIPE_EXTRACTION_PROMPT: Final[str] = """
You are extracting structured data from a Stripe payment-processor monthly
statement PDF. Return ONLY a JSON object — no prose, no markdown.

Required JSON shape:
{
  "summary": {
    "processor": "stripe",
    "business_name": "<merchant business name as printed on the statement, or null>",
    "period_start": "YYYY-MM-DD",
    "period_end": "YYYY-MM-DD",
    "gross_volume": "<money string, positive>",
    "refunds_total": "<money string, positive>",
    "chargebacks_total": "<money string, positive>",
    "fees_total": "<money string, positive>",
    "payouts_total": "<money string, positive>",
    "transaction_count": <integer or null>
  },
  "transactions": [
    {
      "posted_date": "YYYY-MM-DD",
      "description": "<row description as printed>",
      "kind": "gross_charge" | "refund" | "chargeback" | "fee" | "payout" | "adjustment",
      "amount": "<money string, POSITIVE — flow direction is on `kind`>",
      "source_page": <1-indexed page>,
      "source_line": <1-indexed line within the page>
    }
  ]
}

CRITICAL RULES (silent violation here corrupts the audit trail):
- Amounts are ALWAYS positive. The ``kind`` field carries the direction.
- ``source_page`` and ``source_line`` MUST be set on every row. The
  downstream validator rejects rows without them.
- Do NOT compute aggregates. Return the printed totals on `summary`
  exactly as the statement shows them, and the line items as they
  appear in the activity table.
- Stripe line items map to ``kind`` as follows:
    * "Charge" / "Payment" → gross_charge
    * "Refund" → refund
    * "Dispute" / "Chargeback" → chargeback
    * "Fee" / "Stripe fee" / "Processing fee" → fee
    * "Payout" / "Transfer to bank" → payout
    * "Adjustment" / "Balance adjustment" → adjustment
- All money fields must be strings (preserve precision; never floats).
"""


class ProcessorExtractionError(RuntimeError):
    """Raised when the Stripe extraction response can't be parsed."""


def extract_stripe(
    pdf_bytes: bytes, llm: LLMClient
) -> ExtractedProcessorStatement:
    """LLM pass 1 for a Stripe statement.

    Identical shape to the bank-statement extractor's contract:
    bytes in, validated Pydantic model out. Truncation surfaces as
    a validation failure via the empty / partial response shape;
    we don't propagate a separate ``truncated`` bool here because
    the processor validator's tie-out gate catches truncated runs
    via the printed-vs-summed mismatch.
    """
    if len(pdf_bytes) == 0:
        raise ProcessorExtractionError("empty PDF buffer")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise ProcessorExtractionError(
            f"PDF buffer too large: {len(pdf_bytes)} bytes (max {_MAX_PDF_BYTES})"
        )

    try:
        raw, _truncated = llm.extract_raw_json(pdf_bytes, STRIPE_EXTRACTION_PROMPT)
    except ValueError as exc:
        raise ProcessorExtractionError(
            f"LLM returned malformed JSON: {exc}"
        ) from exc

    if "summary" not in raw or "transactions" not in raw:
        raise ProcessorExtractionError(
            f"extraction JSON missing required keys; got {sorted(raw.keys())}"
        )

    payload: dict[str, Any] = {
        "summary": _coerce_summary(raw["summary"]),
        "transactions": [_coerce_row(t) for t in raw["transactions"]],
    }

    try:
        return ExtractedProcessorStatement.model_validate(payload)
    except ValidationError as exc:
        raise ProcessorExtractionError(
            f"extraction payload failed schema validation: {exc}"
        ) from exc


def _coerce_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProcessorExtractionError(
            f"summary must be an object, got {type(value).__name__}"
        )
    out: dict[str, Any] = dict(value)
    for k in (
        "gross_volume",
        "refunds_total",
        "chargebacks_total",
        "fees_total",
        "payouts_total",
    ):
        if k in out and out[k] is not None:
            out[k] = _num_to_str(out[k])
    return out


def _coerce_row(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProcessorExtractionError(
            f"transaction must be an object, got {type(value).__name__}"
        )
    out: dict[str, Any] = dict(value)
    if "amount" in out and out["amount"] is not None:
        # Strip a stray sign if the LLM included one; the model rejects
        # negative values, and the kind field carries flow direction.
        out["amount"] = _abs_str(_num_to_str(out["amount"]))
    return out


def _num_to_str(value: object) -> str:
    """Same precision-preserving cast as the bank extractor."""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _abs_str(value: str) -> str:
    return value.lstrip("-+")


__all__ = [
    "STRIPE_EXTRACTION_PROMPT",
    "ProcessorExtractionError",
    "extract_stripe",
]
