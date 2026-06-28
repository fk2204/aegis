"""Bedrock-driven fallback for Close Lead descriptions without a FINANCIAL block.

Migration 087 added a pure-string parser
(``aegis.close.field_map._parse_close_lead_description``) for Close leads
whose description carries a structured ``FINANCIAL:`` block — a
``KEY: VALUE`` body with operator-typed application fields. That parser
covers ~28 of the current 59 leads. The other ~31 leads have rich
application data in the description text but in a different shape:

* ``DEAL:`` blocks instead of ``FINANCIAL:`` (older phone-call leads).
* Pure free-text (broker pre-screen notes, voice transcripts).
* Partial ``FINANCIAL:`` blocks with new labels the static table hasn't
  picked up (``Current Advances Band``, ``Monthly Deposit Avg``, …).

This module is the fallback. It sends the raw description to Bedrock
with a forced tool-use call that returns one structured JSON blob per
extractable field — money values as Decimal-safe strings, integers as
integers, lists as lists, with a per-field confidence score 0.0-1.0.

The output is NOT auto-written to the live ``stated_*`` columns. Per
CLAUDE.md's "Extraction & automation assists, never replaces judgment"
rule, the orchestrator stages the result in
``merchants.stated_extracted_pending`` (migration 089). The dossier
shows an editable preview; the operator confirms via
``/ui/merchants/{id}/extracted-pending/confirm`` (promotes to live
columns) or discards via ``/extracted-pending/discard`` (clears).

Architecture notes
------------------
* The Bedrock client is the project's standard
  ``aegis.llm.BedrockClient``. Region + model id are sourced from
  settings — the data-residency guard at boot still applies.
* The tool-use call uses ``BedrockClient.invoke_tool_json`` (the same
  path the narrator uses) so the model is FORCED to return JSON in the
  declared schema. No prose, no half-finished sentences.
* Money fields land as ``str`` (Decimal-safe) in the staging blob; the
  confirm route converts to ``Decimal`` before writing the live column.
  We deliberately do NOT round-trip through ``float`` even in JSON.
* The extractor is PURE — it takes a description string and returns a
  dict. It does NOT touch the merchant repository. The orchestration
  script (``scripts/resync_close_leads.py``) decides where to stage the
  result.

Threshold behavior
------------------
* Empty / whitespace-only description → returns ``None`` (the orchestrator
  records this as ``skip_no_description``).
* Very short description (< 80 chars) → returns ``None`` (too little
  signal to bother burning a Bedrock call on).
* Bedrock returns no extractable fields → returns ``None`` so the
  staging blob stays empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.logger import get_logger

_log = get_logger(__name__)


# Minimum description length for the extractor to bother running. Avoids
# burning a Bedrock call on a trivially-short note like "Called, left VM"
# or "See attached PDF". Empirically (prod sample 2026-06-28) every
# description with actual application data is > 200 chars; 80 is a safe
# floor that still catches edge cases without burning quota.
_MIN_DESCRIPTION_LENGTH: Final[int] = 80


# Tool schema for the Bedrock forced tool-use call. Each field is
# OPTIONAL — the model must omit a field when the description doesn't
# carry the value, NOT invent it. The confidence object mirrors the
# fields object key-for-key; missing confidence means the field wasn't
# extracted, which the validator enforces.
_EXTRACTION_TOOL_NAME: Final[str] = "record_application_data"

_EXTRACTION_TOOL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "description": (
                "Application fields extracted from the description. OMIT "
                "a key entirely when the description does not state the "
                "value — never guess, never default. Use the exact key "
                "names below; do not invent new ones."
            ),
            "properties": {
                "monthly_revenue": {
                    "type": "string",
                    "description": (
                        "Merchant-stated MONTHLY gross revenue, as a "
                        "Decimal-safe string (no $, no comma, e.g. "
                        '"175000.00"). NOT annual; NOT daily.'
                    ),
                },
                "avg_monthly_cc_sales": {
                    "type": "string",
                    "description": (
                        "Merchant-stated average MONTHLY credit-card sales, Decimal-safe string."
                    ),
                },
                "requested_amount": {
                    "type": "string",
                    "description": (
                        "Merchant-requested advance amount, Decimal-safe "
                        'string. For a range like "30k-75k" use the LOW '
                        'end ("30000").'
                    ),
                },
                "stated_monthly_deposits": {
                    "type": "integer",
                    "description": (
                        "Merchant-stated COUNT of deposit transactions "
                        "per month. Pure integer, not a money value."
                    ),
                },
                "stated_mca_positions": {
                    "type": "integer",
                    "description": (
                        "Merchant-stated count of existing MCA positions "
                        '(also called "stacks" or "current advances").'
                    ),
                },
                "stated_current_lenders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Names of current MCA lenders the merchant "
                        "disclosed. Each lender as a separate string."
                    ),
                },
                "stated_mca_balance": {
                    "type": "string",
                    "description": (
                        "Merchant-stated TOTAL outstanding MCA balance "
                        "across all positions. Decimal-safe string."
                    ),
                },
                "stated_daily_payment": {
                    "type": "string",
                    "description": (
                        "Merchant-stated daily (or weekly converted to "
                        "daily by dividing by 5) MCA payment. "
                        "Decimal-safe string."
                    ),
                },
                "stated_bank": {
                    "type": "string",
                    "description": (
                        'Merchant-stated primary business bank name (e.g. "TD Bank", "Chase").'
                    ),
                },
                "use_of_funds": {
                    "type": "string",
                    "description": (
                        "Merchant-stated use of funds, free text "
                        '(e.g. "Working capital", "Equipment purchase").'
                    ),
                },
            },
            "additionalProperties": False,
        },
        "confidences": {
            "type": "object",
            "description": (
                "Per-field confidence score, 0.0 to 1.0. MUST have the "
                "same keys as the fields object. 1.0 = field appears "
                'verbatim with a clear label (e.g. "Monthly Revenue: '
                '$175,000"). 0.5 = field is inferred from context. '
                "Below 0.5 you should omit the field entirely instead."
            ),
            "additionalProperties": {"type": "number"},
        },
    },
    "required": ["fields", "confidences"],
    "additionalProperties": False,
}


_EXTRACTION_SYSTEM_PROMPT: Final[str] = (
    "You are extracting MCA application data from a Close CRM Lead "
    "description. The description is operator-typed text — sometimes a "
    "structured FINANCIAL block, sometimes a DEAL block, sometimes a "
    "free-form phone-call note. Your job is to populate the "
    "`record_application_data` tool with ONLY the values the merchant "
    "actually stated.\n\n"
    "HARD RULES:\n"
    "  * NEVER invent a value. If the description doesn't state monthly "
    "revenue, omit `monthly_revenue` from the `fields` object entirely. "
    "Industry-typical defaults are explicitly banned.\n"
    "  * NEVER guess at confidence. Below 0.5 confidence, omit the field.\n"
    '  * NEVER convert units silently. If the merchant says "100k", '
    'emit "100000". If they say "$2M", emit "2000000". If they '
    'say "75-150k" (a range), emit the LOW end ("75000") — that\'s '
    "the broker convention.\n"
    "  * Decimal-safe strings only for money: no $, no comma, no k/M "
    'suffix. "175000" or "175000.00", not "$175,000" or "175k".\n'
    "  * For monthly_revenue specifically: if the merchant gives an "
    "annual figure, do NOT divide by 12. Omit instead — the operator "
    "will catch it.\n"
    "  * stated_monthly_deposits is a COUNT (integer 0-100), not money. "
    'If the description says "$150,000 monthly deposit avg" that is a '
    "dollar amount, not a count — do NOT put it in this field.\n"
    "  * stated_mca_positions is an integer count (0, 1, 2, 3, ...).\n"
    "  * stated_current_lenders is a list of lender names — split on "
    'commas, newlines, or "and". Strip dollar amounts the operator '
    'appended ("Revenued = 91,106.09" → "Revenued").\n'
    "  * The `confidences` object MUST have the same keys as `fields`. "
    "If `fields.monthly_revenue` is present, `confidences.monthly_revenue` "
    "must also be present."
)


_EXTRACTION_USER_PROMPT_TEMPLATE: Final[str] = (
    "Extract MCA application data from the following Close Lead "
    "description. Return ONLY the fields the merchant actually stated.\n\n"
    "─── BEGIN DESCRIPTION ───\n"
    "{description}\n"
    "─── END DESCRIPTION ───"
)


# ----------------------------------------------------------------------
# LLM client Protocol — narrower than aegis.llm.LLMClient so test stubs
# only need to implement the one method we actually call.
# ----------------------------------------------------------------------


class _ExtractorLLMClient(Protocol):
    def invoke_tool_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], str]: ...


# ----------------------------------------------------------------------
# Output Pydantic shape — strict, no extras.
# ----------------------------------------------------------------------


class ExtractedFieldsPayload(BaseModel):
    """Validated staging payload for ``merchants.stated_extracted_pending``.

    The orchestrator builds this from the LLM response and the model
    serialises to JSON-safe types (``str`` for money, ``int`` for
    counts, ``list[str]`` for lenders) before writing to Supabase.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fields: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "AEGIS-side field names keyed to extracted values. Money is "
            "Decimal-safe string; counts are int; lenders are list[str]."
        ),
    )
    confidences: dict[str, float] = Field(
        default_factory=dict,
        description="Per-field confidence 0.0-1.0. Keys mirror ``fields``.",
    )
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_chars: int = Field(ge=0, description="Length of the description seen by the LLM.")
    model_id: str = Field(
        min_length=1,
        description="Bedrock model id used (e.g. us.anthropic.claude-sonnet-4-6).",
    )


# ----------------------------------------------------------------------
# Public surface
# ----------------------------------------------------------------------


def extract_from_description(
    description: str | None,
    *,
    llm_client: _ExtractorLLMClient,
) -> ExtractedFieldsPayload | None:
    """Run the Bedrock extraction over a Close Lead description.

    Returns ``None`` when:
      * ``description`` is ``None`` / empty / whitespace-only,
      * ``description`` is shorter than ``_MIN_DESCRIPTION_LENGTH``,
      * Bedrock returns no usable fields (empty fields dict, or a shape
        that fails the post-extraction sanitiser).

    Returns an ``ExtractedFieldsPayload`` otherwise. The caller writes
    ``payload.model_dump(mode="json")`` to
    ``merchants.stated_extracted_pending``.

    Raises on Bedrock-side hard failures (DataResidencyError,
    APIStatusError outside the retry filter, etc.) — those are
    configuration bugs the caller should NOT swallow.
    """
    if description is None:
        return None
    body = description.strip()
    if len(body) < _MIN_DESCRIPTION_LENGTH:
        return None

    raw, model_id = llm_client.invoke_tool_json(
        system_prompt=_EXTRACTION_SYSTEM_PROMPT,
        user_prompt=_EXTRACTION_USER_PROMPT_TEMPLATE.format(description=body),
        tool_name=_EXTRACTION_TOOL_NAME,
        tool_schema=_EXTRACTION_TOOL_SCHEMA,
        max_tokens=2048,
        # Temperature 0 — extraction is deterministic; we want the same
        # input to produce the same output across re-runs so the operator
        # can compare a previous staging blob against a re-extract.
        temperature=0.0,
    )

    sanitised = _sanitise_extraction(raw)
    if not sanitised["fields"]:
        # Model returned nothing usable — don't stage an empty blob.
        return None

    return ExtractedFieldsPayload(
        fields=sanitised["fields"],
        confidences=sanitised["confidences"],
        source_chars=len(body),
        model_id=model_id,
    )


# ----------------------------------------------------------------------
# Internal: defensive sanitiser
# ----------------------------------------------------------------------


# AEGIS-side field names the staging payload accepts. Names match the
# ``merchants.stated_*`` columns so the confirm route can promote with a
# simple dict-comprehension. Adding a new field here requires:
#   1. Updating the tool schema above.
#   2. Adding the column to ``MerchantRow`` + ``_row_to_merchant`` /
#      ``_merchant_to_payload``.
#   3. Adding the field to the confirm route's column allow list.
_ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "monthly_revenue",
        "avg_monthly_cc_sales",
        "requested_amount",
        "stated_monthly_deposits",
        "stated_mca_positions",
        "stated_current_lenders",
        "stated_mca_balance",
        "stated_daily_payment",
        "stated_bank",
        "use_of_funds",
    }
)

_MONEY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "monthly_revenue",
        "avg_monthly_cc_sales",
        "requested_amount",
        "stated_mca_balance",
        "stated_daily_payment",
    }
)

_INT_FIELDS: Final[frozenset[str]] = frozenset({"stated_monthly_deposits", "stated_mca_positions"})

_LIST_FIELDS: Final[frozenset[str]] = frozenset({"stated_current_lenders"})

_STR_FIELDS: Final[frozenset[str]] = frozenset({"stated_bank", "use_of_funds"})


def _sanitise_extraction(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce the LLM tool-use payload.

    Returns ``{"fields": {...}, "confidences": {...}}`` with every value
    in the AEGIS-side type contract:

      * Money fields → ``str`` (Decimal-safe — round-tripped through
        ``Decimal`` to reject "garbage", "1.2.3", or accidental floats).
      * Integer fields → ``int`` (0 or positive).
      * List fields → ``list[str]`` (deduplicated, stripped, empties
        dropped).
      * String fields → ``str`` (stripped).
      * Confidences → ``float`` in [0.0, 1.0]; out-of-range or missing
        causes the field to be DROPPED rather than the whole row to fail.

    Drops any field name the LLM invented outside ``_ALLOWED_FIELDS``.
    """
    raw_fields = raw.get("fields") or {}
    raw_confidences = raw.get("confidences") or {}
    if not isinstance(raw_fields, dict) or not isinstance(raw_confidences, dict):
        _log.warning("description_extractor.malformed_tool_payload")
        return {"fields": {}, "confidences": {}}

    out_fields: dict[str, Any] = {}
    out_confidences: dict[str, float] = {}

    for name, value in raw_fields.items():
        if name not in _ALLOWED_FIELDS:
            _log.info("description_extractor.skipping_unknown_field name=%s", name)
            continue

        confidence_raw = raw_confidences.get(name)
        if not isinstance(confidence_raw, (int, float)):
            _log.info("description_extractor.skipping_field_missing_confidence name=%s", name)
            continue
        confidence = float(confidence_raw)
        if not (0.0 <= confidence <= 1.0):
            _log.info(
                "description_extractor.skipping_field_out_of_range_confidence name=%s value=%s",
                name,
                confidence,
            )
            continue

        coerced = _coerce_field(name, value)
        if coerced is None:
            continue

        out_fields[name] = coerced
        out_confidences[name] = confidence

    return {"fields": out_fields, "confidences": out_confidences}


def _coerce_field(name: str, value: Any) -> Any:  # noqa: ANN401 — heterogeneous LLM payload
    """Coerce one extracted value to the AEGIS-side type contract.

    Returns ``None`` to signal "drop this field" — never raises. The
    sanitiser uses the return value as a presence sentinel.
    """
    if value is None:
        return None

    if name in _MONEY_FIELDS:
        # Tool schema declares ``string`` for money, but defensively
        # accept ``int``/``float`` (some Bedrock retries emit a numeric
        # type even when the schema says string). Always round-trip
        # through ``Decimal`` so garbage / multi-decimal / unit-suffixed
        # text is rejected.
        candidate = str(value).strip().replace("$", "").replace(",", "")
        if not candidate:
            return None
        try:
            decimal_value = Decimal(candidate)
        except (InvalidOperation, ValueError):
            _log.info("description_extractor.dropping_unparseable_money name=%s", name)
            return None
        if decimal_value < 0:
            return None
        # Normalise to a Decimal-safe string. ``str(Decimal)`` preserves
        # the operator-typed precision (e.g. ``"175000.00"`` stays that
        # way rather than collapsing to ``"175000"``).
        return str(decimal_value)

    if name in _INT_FIELDS:
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
        if not deduped:
            return None
        return deduped

    if name in _STR_FIELDS:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    # Unknown field shape — shouldn't reach here given the allow list,
    # but defensively drop.
    return None


__all__ = ["ExtractedFieldsPayload", "extract_from_description"]
