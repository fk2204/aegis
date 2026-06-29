"""Funder guidelines PDF -> staging JSONB extraction (build plan 8.1).

Distinct from `aegis.funders.extract.extract_funder_guidelines` (which
populates the live FunderRow on operator-confirmed re-extraction). This
module's output is a STAGING blob: `FunderGuidelinesExtraction` rides on
the migration-096 `funders.guidelines_data` JSONB column and never
auto-overrides operator-curated columns. The operator promotes
individual fields to the live FunderRow via a separate UI flow.

Pipeline shape:

  1. Caller validates file size BEFORE reading into memory (max 25 MB).
  2. PDF bytes go through `aegis.llm.LLMClient.extract_raw_json` with the
     strict prompt below; the model returns the JSON object directly.
  3. The raw dict is sanitised: Decimal-safe round-trip for money fields,
     int coercion for counts, list[str] for excluded_*, Literal check on
     stacking_policy. Fields whose confidence is below 0.5 are dropped.
  4. The sanitised payload is the JSONB blob written to
     `funders.guidelines_data`; `funders.guidelines_uploaded_at` is
     stamped to `now()`.

The LLMClient is injected so tests can stub canned responses (see
`tests/funders/conftest.py::_StubLLM`).
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aegis.llm import LLMClient

_log = logging.getLogger(__name__)


_MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024


# Forced JSON shape requested from Bedrock. The exact prompt text is
# load-bearing — operator agent task spec verbatim.
GUIDELINES_EXTRACTION_PROMPT: Final[str] = (
    "Extract underwriting criteria from this funder guidelines document. "
    "Return JSON: {min_revenue, min_fico, min_tib_months, max_positions, "
    "stacking_policy, excluded_industries: [], excluded_states: [], "
    "max_advance_amount, notes}\n\n"
    "Field rules:\n"
    "  * min_revenue, max_advance_amount: dollar amount as a STRING — no "
    '    commas, no currency symbol (e.g. "25000.00"). NEVER a number. '
    "    Decimal-safe round-trip is enforced AEGIS-side.\n"
    "  * min_fico: integer (300-850) or null.\n"
    "  * min_tib_months: integer months in business or null.\n"
    "  * max_positions: integer maximum stacked MCA positions or null.\n"
    '  * stacking_policy: one of "allowed", "not_allowed", '
    '    "case_by_case", or null.\n'
    "  * excluded_industries, excluded_states: arrays of strings; empty "
    "    arrays when not specified. State codes as USPS two-letter "
    "    (CA, NY, VT, ...).\n"
    "  * notes: short prose summary of policy nuances that don't fit the "
    "    structured fields (renewals, position policy edge cases, etc.). "
    "    Empty string if nothing extra.\n\n"
    'Also include a sibling top-level object "confidences" mirroring '
    "the field names with floats in [0.0, 1.0]. Confidence below 0.5 = "
    "the value is a guess; the AEGIS-side sanitiser will drop the "
    "field. Do NOT invent values to fill the schema — set confidence "
    "below 0.5 (or omit the field entirely) when the document does not "
    "support the value.\n\n"
    "Return ONLY the JSON object. No prose before or after."
)


StackingPolicy = Literal["allowed", "not_allowed", "case_by_case"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class FunderGuidelinesExtraction(_StrictModel):
    """Sanitised staging payload for the migration-096 JSONB column.

    Money fields are Decimal-safe strings (never floats) per CLAUDE.md
    money discipline. Integer fields are non-negative. List fields are
    deduplicated, stripped. Stacking policy is a closed Literal — the
    Bedrock value is rejected outright if it lies outside the set.

    Distinct from `aegis.funders.models.FunderGuidelineExtraction` —
    that one targets the live FunderRow under the existing re-extract
    flow; this one is the read-only side-channel.
    """

    min_revenue: str | None = None
    min_fico: int | None = Field(default=None, ge=300, le=850)
    min_tib_months: int | None = Field(default=None, ge=0)
    max_positions: int | None = Field(default=None, ge=0)
    stacking_policy: StackingPolicy | None = None
    excluded_industries: list[str] = Field(default_factory=list)
    excluded_states: list[str] = Field(default_factory=list)
    max_advance_amount: str | None = None
    notes: str = ""

    @property
    def fields_populated_count(self) -> int:
        """Count of non-empty / non-null fields. Surfaces on the audit row.

        A funder PDF that lands zero fields populated is a strong signal
        the extraction failed silently and the operator should re-upload
        a better source document.
        """
        count = 0
        if self.min_revenue is not None:
            count += 1
        if self.min_fico is not None:
            count += 1
        if self.min_tib_months is not None:
            count += 1
        if self.max_positions is not None:
            count += 1
        if self.stacking_policy is not None:
            count += 1
        if self.excluded_industries:
            count += 1
        if self.excluded_states:
            count += 1
        if self.max_advance_amount is not None:
            count += 1
        if self.notes:
            count += 1
        return count


class GuidelinesExtractionError(RuntimeError):
    """Raised when the LLM response cannot be sanitised into FunderGuidelinesExtraction."""


# Field-name -> coercion-bucket maps. Keeps the per-field sanitiser
# branchless at call time.
_MONEY_FIELDS: Final[frozenset[str]] = frozenset({"min_revenue", "max_advance_amount"})
_INT_FIELDS: Final[frozenset[str]] = frozenset({"min_fico", "min_tib_months", "max_positions"})
_LIST_FIELDS: Final[frozenset[str]] = frozenset({"excluded_industries", "excluded_states"})
_STR_FIELDS: Final[frozenset[str]] = frozenset({"notes"})
_LITERAL_FIELDS: Final[frozenset[str]] = frozenset({"stacking_policy"})

_ALLOWED_FIELDS: Final[frozenset[str]] = (
    _MONEY_FIELDS | _INT_FIELDS | _LIST_FIELDS | _STR_FIELDS | _LITERAL_FIELDS
)

_VALID_STACKING_POLICIES: Final[frozenset[str]] = frozenset(
    {"allowed", "not_allowed", "case_by_case"}
)

# Confidence floor — mirrors `aegis.close.description_extractor`. Fields
# whose confidence is < 0.5 are dropped (the LLM is told to omit them
# itself; defensive enforcement here protects against prompt drift).
_CONFIDENCE_FLOOR: Final[float] = 0.5


def extract_guidelines_from_pdf(
    pdf_bytes: bytes,
    llm: LLMClient,
) -> FunderGuidelinesExtraction:
    """Extract underwriting criteria from a funder guidelines PDF.

    Raises ``GuidelinesExtractionError`` on:
      * empty / oversized PDF buffer (size check happens at the route
        boundary too, but defended here for symmetry with extract.py),
      * malformed JSON from the LLM,
      * truncated output (max_tokens hit — the document is too long for
        the model budget),
      * a sanitised payload that fails Pydantic validation (e.g.
        ``min_fico`` outside [300, 850]).
    """
    if len(pdf_bytes) == 0:
        raise GuidelinesExtractionError("empty PDF buffer")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise GuidelinesExtractionError(
            f"PDF buffer too large: {len(pdf_bytes)} bytes (max {_MAX_PDF_BYTES})"
        )

    try:
        raw, truncated = llm.extract_raw_json(pdf_bytes, GUIDELINES_EXTRACTION_PROMPT)
    except ValueError as exc:
        raise GuidelinesExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    if truncated:
        raise GuidelinesExtractionError(
            "LLM extraction was truncated at max_tokens — funder guideline "
            "document exceeds the model's output budget; try a smaller "
            "document or summary."
        )

    sanitised = _sanitise_extraction(raw)

    try:
        return FunderGuidelinesExtraction.model_validate(sanitised)
    except ValidationError as exc:
        raise GuidelinesExtractionError(f"FunderGuidelinesExtraction validation: {exc}") from exc


def _sanitise_extraction(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce the LLM payload into the staging JSONB shape.

    Returns a dict ready for ``FunderGuidelinesExtraction.model_validate``.

      * Money fields -> Decimal-safe string (round-tripped through
        ``Decimal`` to reject garbage / multi-decimal / float artefacts).
      * Integer fields -> int (Pydantic enforces the per-field range).
      * List fields -> deduplicated, stripped list[str].
      * stacking_policy -> Literal check; anything outside the closed
        set is dropped.
      * notes -> stripped str.

    Drops any field name the LLM invented outside ``_ALLOWED_FIELDS``.
    Drops fields whose paired confidence is below ``_CONFIDENCE_FLOOR``.
    """
    raw_fields = raw if isinstance(raw, dict) else {}
    raw_confidences = raw_fields.get("confidences") or {}
    if not isinstance(raw_confidences, dict):
        raw_confidences = {}

    out: dict[str, Any] = {}

    for name, value in raw_fields.items():
        if name == "confidences":
            continue
        if name not in _ALLOWED_FIELDS:
            _log.info("guidelines_extract.skipping_unknown_field name=%s", name)
            continue

        # Confidence floor — same pattern as
        # `aegis.close.description_extractor`. Below 0.5 = drop the field.
        confidence_raw = raw_confidences.get(name)
        if isinstance(confidence_raw, (int, float)):
            confidence = float(confidence_raw)
            if not (0.0 <= confidence <= 1.0):
                _log.info(
                    "guidelines_extract.dropping_out_of_range_confidence name=%s value=%s",
                    name,
                    confidence,
                )
                continue
            if confidence < _CONFIDENCE_FLOOR:
                _log.info(
                    "guidelines_extract.dropping_low_confidence name=%s value=%s",
                    name,
                    confidence,
                )
                continue
        # Confidence missing = treat as 0 and drop, EXCEPT for `notes`
        # which carries operator-visible context the model didn't think
        # was a structured field. Notes is always preserved if present.
        elif name != "notes":
            _log.info("guidelines_extract.dropping_missing_confidence name=%s", name)
            continue

        coerced = _coerce_field(name, value)
        if coerced is None:
            continue
        out[name] = coerced

    return out


def _coerce_field(name: str, value: Any) -> Any:  # noqa: ANN401 — heterogeneous LLM payload
    """Coerce one extracted value to the staging-JSONB type contract.

    Returns ``None`` to signal "drop this field" — never raises. The
    sanitiser uses the return value as a presence sentinel.
    """
    if value is None:
        return None

    if name in _MONEY_FIELDS:
        # Tool schema asked for STRING for money, but defensively accept
        # int / float (some retries emit numeric types even when the
        # prompt says string). Always round-trip through Decimal so
        # garbage / "1.2.3" / "$5K" is rejected.
        candidate = str(value).strip().replace("$", "").replace(",", "")
        if not candidate:
            return None
        try:
            decimal_value = Decimal(candidate)
        except (InvalidOperation, ValueError):
            _log.info("guidelines_extract.dropping_unparseable_money name=%s", name)
            return None
        if decimal_value < 0:
            return None
        return str(decimal_value)

    if name in _INT_FIELDS:
        # bool is a subclass of int — guard so True/False never lands.
        if isinstance(value, bool):
            return None
        try:
            int_value = int(value)
        except (TypeError, ValueError):
            return None
        if int_value < 0:
            return None
        return int_value

    if name in _LIST_FIELDS:
        if not isinstance(value, list):
            return None
        deduped: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            cleaned = item.strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(cleaned)
        return deduped  # empty list is a valid value (operator sees [] vs missing)

    if name in _STR_FIELDS:
        if not isinstance(value, str):
            return None
        return value.strip()

    if name in _LITERAL_FIELDS:
        if not isinstance(value, str):
            return None
        cleaned = value.strip().lower()
        if cleaned not in _VALID_STACKING_POLICIES:
            _log.info(
                "guidelines_extract.dropping_invalid_stacking_policy value=%s",
                cleaned,
            )
            return None
        return cleaned

    # Defensive — unreachable given _ALLOWED_FIELDS guard above.
    return None


__all__ = [
    "GUIDELINES_EXTRACTION_PROMPT",
    "FunderGuidelinesExtraction",
    "GuidelinesExtractionError",
    "extract_guidelines_from_pdf",
]
