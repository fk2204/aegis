"""Funder guideline extraction — PDF in, FunderGuidelineExtraction out.

Single-pass: feeds the criteria PDF + extraction prompt to Bedrock, parses
the JSON response into a Pydantic-validated `FunderGuidelineExtraction`.
The operator reviews per-field confidence in the dashboard before
upserting into the funder repository.

The LLM client is injected (`LLMClient` Protocol) so tests can stub canned
responses. Production wiring uses `BedrockClient` from `aegis.llm`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from aegis.funders.models import FunderGuidelineExtraction, FunderRow
from aegis.funders.prompts import FUNDER_GUIDELINE_EXTRACTION_PROMPT
from aegis.llm import LLMClient

_MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024


class FunderExtractionError(RuntimeError):
    """Raised when the LLM response cannot be parsed into FunderGuidelineExtraction."""


def extract_funder_guidelines(
    pdf_bytes: bytes,
    llm: LLMClient,
) -> FunderGuidelineExtraction:
    """Extract a funder's underwriting criteria from a guideline PDF."""
    if len(pdf_bytes) == 0:
        raise FunderExtractionError("empty PDF buffer")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise FunderExtractionError(
            f"PDF buffer too large: {len(pdf_bytes)} bytes (max {_MAX_PDF_BYTES})"
        )

    try:
        raw, truncated = llm.extract_raw_json(pdf_bytes, FUNDER_GUIDELINE_EXTRACTION_PROMPT)
    except ValueError as exc:
        raise FunderExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    if truncated:
        raise FunderExtractionError(
            "LLM extraction was truncated at max_tokens — funder guideline PDF "
            "exceeds the model's output budget; try a smaller PDF or summary."
        )

    if "draft" not in raw:
        raise FunderExtractionError(
            f"extraction JSON missing 'draft' key; got keys={sorted(raw.keys())}"
        )

    draft_payload = _coerce_draft(raw["draft"])
    confidence = _coerce_confidence(raw.get("confidence_by_field", {}))
    unparseable = _coerce_str_list(raw.get("unparseable_fragments", []))
    overall = _coerce_int(raw.get("overall_confidence", 0))

    # Stamp provenance: when extraction ran + the PDF hash so re-extraction
    # is detectable.
    draft_payload["guidelines_extracted_at"] = datetime.now(UTC)
    draft_payload["guidelines_source_pdf_hash"] = hashlib.sha256(pdf_bytes).hexdigest()

    try:
        draft = FunderRow.model_validate(draft_payload)
    except ValidationError as exc:
        raise FunderExtractionError(f"draft FunderRow failed validation: {exc}") from exc

    try:
        return FunderGuidelineExtraction(
            draft=draft,
            confidence_by_field=confidence,
            unparseable_fragments=unparseable,
            overall_confidence=overall,
        )
    except ValidationError as exc:
        raise FunderExtractionError(f"FunderGuidelineExtraction validation: {exc}") from exc


# -- coercion helpers --------------------------------------------------------


def _coerce_draft(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FunderExtractionError(f"draft must be an object, got {type(value).__name__}")
    out: dict[str, Any] = dict(value)

    # Money fields: convert numbers to strings so Pydantic Decimal stays float-free.
    for key in (
        "min_monthly_revenue",
        "min_avg_daily_balance",
        "min_advance",
        "max_advance",
        "typical_factor_low",
        "typical_factor_high",
        "typical_holdback_low",
        "typical_holdback_high",
    ):
        if key in out and out[key] is not None:
            out[key] = _num_to_str(out[key])

    # Tuple-shaped fields
    for key in ("excluded_industries", "excluded_states"):
        seq = out.get(key)
        if seq is None:
            out[key] = ()
        elif isinstance(seq, list):
            out[key] = tuple(str(v) for v in seq)

    # `name` falls back to "Unknown Funder" if missing — operator must
    # rename before saving, but extraction shouldn't fail validation here.
    if not out.get("name"):
        out["name"] = "Unknown Funder"

    return out


def _coerce_confidence(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        out[key] = _coerce_int(raw)
    return out


def _coerce_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, min(100, value))
    if isinstance(value, float):
        return max(0, min(100, int(value)))
    if isinstance(value, str):
        try:
            return max(0, min(100, int(float(value))))
        except ValueError:
            return 0
    return 0


def _num_to_str(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


__all__ = ["FunderExtractionError", "extract_funder_guidelines"]
