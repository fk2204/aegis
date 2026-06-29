"""Bedrock-vision equipment invoice / quote extractor.

Inputs an equipment quote / invoice PDF (filename routed via
``detect_equipment_document``) and returns a validated
``EquipmentInvoiceResult`` carrying description, make, model, year,
condition, serial number, VIN, vendor name, and total cost.

Architecture mirrors ``aegis.close.description_extractor``:

* Uses ``BedrockClient.invoke_tool_json`` (forced tool-use) so the
  model must return a single structured JSON blob. No prose, no
  partial output, no free-form fallback.
* ``temperature=0`` so re-running the extractor on the same quote
  yields the same fields — extraction is a deterministic pre-fill,
  not a generative task.
* Tool schema declares ``total_cost`` as a Decimal-safe string. The
  sanitiser round-trips through ``Decimal`` and rejects garbage the
  same way ``aegis.close.description_extractor._coerce_field`` does.
* VIN normalisation: 17 characters, no I / O / Q. Anything else is
  dropped (the field is never silently shipped with a malformed value).

Per CLAUDE.md's "Extraction & automation assists, never replaces
judgment" rule, the result is staged on
``merchants.equipment_details`` and surfaced on the dossier; the
operator confirms or edits via the existing merchant edit surface.
The extractor is PURE — it takes a PDF path and an LLM client and
returns a model, never touching the merchant repository.
"""

from __future__ import annotations

import base64
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import ValidationError

from aegis.logger import get_logger
from aegis.parser.equipment.models import EquipmentInvoiceResult

_log = get_logger(__name__)


# Tool name + schema for the forced Bedrock tool-use call. Each
# OPTIONAL field MUST be omitted entirely when the quote does not
# state the value (operator-principle 4: empty better than wrong).
_TOOL_NAME: Final[str] = "record_equipment_invoice"

_TOOL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": (
                "One-line description of the equipment being financed. "
                "Verbatim from the quote when possible — e.g. "
                '"2023 Kenworth T880 Day Cab" or "Bobcat S650 Skid Steer Loader". '
                "REQUIRED — an equipment quote without a clear description "
                "is not extractable."
            ),
        },
        "make": {
            "type": "string",
            "description": (
                'Manufacturer name (e.g. "Kenworth", "Bobcat", "Caterpillar"). Omit if not stated.'
            ),
        },
        "model": {
            "type": "string",
            "description": (
                'Model designation (e.g. "T880", "S650", "320GC"). Omit if not stated.'
            ),
        },
        "year": {
            "type": "integer",
            "description": (
                "Model year, four-digit integer (1900-2100). Omit if not stated. "
                "Do NOT use the invoice date as a fallback for model year."
            ),
        },
        "condition": {
            "type": "string",
            "enum": ["new", "used", "refurbished"],
            "description": (
                'Equipment condition. "new" for first-sale equipment; "used" '
                'for any second-hand sale; "refurbished" only when the quote '
                "explicitly uses the word refurbished / remanufactured / rebuilt. "
                "Omit if not stated."
            ),
        },
        "serial_number": {
            "type": "string",
            "description": (
                'Serial number ("SN", "S/N", "Serial #"). Omit if not stated. '
                "Do NOT confuse with the VIN — VINs are vehicle-specific and "
                "go in the vin field."
            ),
        },
        "vin": {
            "type": "string",
            "description": (
                "Vehicle Identification Number — 17 characters, no I / O / Q. "
                "Omit if not stated or if the printed VIN is fewer than 17 "
                "characters."
            ),
        },
        "vendor_name": {
            "type": "string",
            "description": (
                "Vendor / dealer / seller business name issuing the quote "
                "(e.g. dealership name in the letterhead). Omit if not stated."
            ),
        },
        "total_cost": {
            "type": "string",
            "description": (
                "Total cost of the equipment as a Decimal-safe string "
                "(no $, no comma, no k/M suffix). Use the FINAL total "
                "including tax and freight when stated; otherwise the "
                'subtotal. Example: "52500.00", not "$52,500".'
            ),
        },
    },
    "required": ["description", "total_cost"],
    "additionalProperties": False,
}


_SYSTEM_PROMPT: Final[str] = (
    "You are extracting equipment-purchase data from a quote or invoice "
    "PDF. Your job is to populate the `record_equipment_invoice` tool "
    "with ONLY values the document explicitly states.\n\n"
    "HARD RULES:\n"
    "  * NEVER invent a value. If the quote does not state the year, "
    "omit `year`. Industry-typical defaults are explicitly banned.\n"
    "  * NEVER convert units silently. Total cost is the printed "
    "number; do not annualise, do not divide by a term, do not "
    'compute a monthly payment. "$52,500" emits as "52500.00".\n'
    "  * `total_cost` must be a Decimal-safe string — no $, no comma, "
    'no k/M suffix. "52500" or "52500.00", never "$52,500" or "52.5k".\n'
    "  * VIN must be 17 characters and contain no I, O, or Q. Anything "
    "shorter or with banned letters MUST be omitted entirely.\n"
    "  * Serial numbers are NOT VINs. Trucks have VINs; skid steers / "
    "excavators / generators have serial numbers. Use the dedicated "
    "field per the source document — never copy a serial into vin.\n"
    "  * If the document is not actually an equipment quote / invoice "
    "(e.g. a bank statement misrouted here), populate description = "
    '"(not an equipment quote)" and total_cost = "0.00"; the post-'
    "extraction sanitiser will drop the result."
)


_USER_PROMPT: Final[str] = (
    "Extract the equipment-purchase data from the attached PDF. "
    "Return ONLY the fields the document explicitly states. Omit "
    "every field that is not present in the source."
)


# ----------------------------------------------------------------------
# LLM client Protocol — mirrors the description_extractor approach so
# test stubs only need to implement the one method we call.
# ----------------------------------------------------------------------


class _EquipmentLLMClient(Protocol):
    def invoke_tool_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        pdf_bytes: bytes | None = None,
    ) -> tuple[dict[str, Any], str]: ...


# ----------------------------------------------------------------------
# Public extraction entrypoint
# ----------------------------------------------------------------------


_EQUIPMENT_FILENAME_TOKENS: Final[tuple[str, ...]] = (
    "quote",
    "equipment",
    "vehicle",
    "machinery",
    "invoice",
)


def detect_equipment_document(filename: str) -> bool:
    """Return True when the filename looks like an equipment invoice /
    quote. A/R aging wins precedence: if the filename ALSO carries an
    aging / receivable / ar_ / a_r token, this returns False so the
    document routes to the A/R parser instead.

    Pure substring match on the lowercased basename.
    """
    if not filename:
        return False
    from aegis.parser.ar_aging.extract import detect_ar_aging_filename

    if detect_ar_aging_filename(filename):
        return False
    name = filename.lower()
    return any(token in name for token in _EQUIPMENT_FILENAME_TOKENS)


def extract_equipment_details(
    file_path: str,
    *,
    llm_client: _EquipmentLLMClient,
) -> EquipmentInvoiceResult | None:
    """Run the Bedrock vision extraction over an equipment quote / invoice PDF.

    Returns a validated ``EquipmentInvoiceResult`` on success.
    Returns ``None`` when:

      * The PDF body is empty / unreadable,
      * The sanitiser rejects ``total_cost`` (non-numeric / negative),
      * Pydantic validation fails (missing required ``description`` or
        ``total_cost`` after sanitising).

    Raises on Bedrock-side hard failures (DataResidencyError,
    non-retryable APIStatusError, etc.) — those are configuration bugs
    the caller should NOT swallow.

    ``llm_client`` is injected so tests can pass a stub. Production
    callers pass a ``BedrockClient`` — the equipment vision path uses
    the same ``invoke_tool_json`` surface the narrator + Close
    description-extractor use; a ``pdf_bytes`` kwarg is plumbed through
    so the client can attach the PDF as a document block.
    """
    path = Path(file_path)
    if not path.exists():
        _log.warning("equipment_extractor.missing_file path=%s", file_path)
        return None
    pdf_bytes = path.read_bytes()
    if not pdf_bytes:
        return None

    raw, _model_id = llm_client.invoke_tool_json(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_USER_PROMPT,
        tool_name=_TOOL_NAME,
        tool_schema=_TOOL_SCHEMA,
        max_tokens=2048,
        # Temperature 0 — extraction is deterministic; the same quote
        # should produce the same fields across re-runs so an operator
        # comparing two extracts sees stable output.
        temperature=0.0,
        pdf_bytes=pdf_bytes,
    )

    sanitised = _sanitise_extraction(raw)
    if sanitised is None:
        return None

    try:
        return EquipmentInvoiceResult(**sanitised)
    except ValidationError as exc:
        _log.info("equipment_extractor.validation_failed errors=%s", exc.errors()[:3])
        return None


# ----------------------------------------------------------------------
# Internal: defensive sanitiser
# ----------------------------------------------------------------------


# VIN: 17 chars, no I/O/Q. The North American standard since 1981 —
# anything else is either a serial number the model misclassified or a
# typo the operator should re-enter manually. Drop rather than ship.
_VIN_FORBIDDEN_LETTERS: Final[frozenset[str]] = frozenset({"I", "O", "Q"})
_VIN_REQUIRED_LENGTH: Final[int] = 17


def _sanitise_extraction(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and coerce the raw tool-use payload.

    Returns a dict suitable for ``EquipmentInvoiceResult(**dict)`` or
    ``None`` when the result is non-recoverable (missing description,
    unparseable total_cost, "not an equipment quote" sentinel).
    """
    if not isinstance(raw, dict):
        return None

    description = _coerce_str(raw.get("description"))
    if not description:
        return None
    if description.lower().startswith("(not an equipment quote"):
        # The model itself signalled this is not actually an equipment
        # quote. Drop the whole result.
        return None

    total_cost = _coerce_money(raw.get("total_cost"))
    if total_cost is None or total_cost <= 0:
        return None

    out: dict[str, Any] = {
        "description": description,
        "total_cost": total_cost,
    }
    make = _coerce_str(raw.get("make"))
    if make:
        out["make"] = make
    model_value = _coerce_str(raw.get("model"))
    if model_value:
        out["model"] = model_value
    year = _coerce_year(raw.get("year"))
    if year is not None:
        out["year"] = year
    condition = _coerce_condition(raw.get("condition"))
    if condition:
        out["condition"] = condition
    serial_number = _coerce_str(raw.get("serial_number"))
    if serial_number:
        out["serial_number"] = serial_number
    vin = _coerce_vin(raw.get("vin"))
    if vin:
        out["vin"] = vin
    vendor_name = _coerce_str(raw.get("vendor_name"))
    if vendor_name:
        out["vendor_name"] = vendor_name
    return out


def _coerce_str(value: Any) -> str | None:  # noqa: ANN401 — heterogeneous LLM payload
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_money(value: Any) -> Decimal | None:  # noqa: ANN401 — heterogeneous LLM payload
    if value is None:
        return None
    candidate = str(value).strip().replace("$", "").replace(",", "")
    if not candidate:
        return None
    try:
        decimal_value = Decimal(candidate)
    except (InvalidOperation, ValueError):
        _log.info("equipment_extractor.dropping_unparseable_money raw=%r", value)
        return None
    if decimal_value < 0:
        return None
    # Quantize to 2 decimal places so the numeric(14,2) round-trip is
    # exact (Pydantic's max_digits=14 / decimal_places=2 would reject
    # a 3-decimal Decimal otherwise).
    return decimal_value.quantize(Decimal("0.01"))


def _coerce_year(value: Any) -> int | None:  # noqa: ANN401 — heterogeneous LLM payload
    if isinstance(value, bool):
        # bool subclasses int — strict-equality reject.
        return None
    if isinstance(value, int):
        return value if 1900 <= value <= 2100 else None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned.isdigit():
            return None
        as_int = int(cleaned)
        return as_int if 1900 <= as_int <= 2100 else None
    return None


def _coerce_condition(value: Any) -> str | None:  # noqa: ANN401 — heterogeneous LLM payload
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in ("new", "used", "refurbished"):
        return lowered
    return None


def _coerce_vin(value: Any) -> str | None:  # noqa: ANN401 — heterogeneous LLM payload
    """Validate VIN — 17 chars, no I/O/Q. Drop on any mismatch.

    North American VIN standard since 1981 (FMVSS 565). Operators
    misread VIN digits often enough that shipping a malformed VIN onto
    the dossier creates real downstream confusion when the funder
    checks the VIN — so the gate is strict-or-omit, never best-effort.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper().replace(" ", "").replace("-", "")
    if len(candidate) != _VIN_REQUIRED_LENGTH:
        return None
    if not candidate.isalnum():
        return None
    for ch in candidate:
        if ch in _VIN_FORBIDDEN_LETTERS:
            return None
    return candidate


# ----------------------------------------------------------------------
# Production helper — wraps ``BedrockClient.invoke_tool_json`` so callers
# can pass a PDF directly. The narrow Protocol above accepts the
# ``pdf_bytes`` kwarg; the production wrapper attaches the document
# block in the same shape ``extract_raw_json`` uses.
# ----------------------------------------------------------------------


def encode_pdf_for_bedrock(pdf_bytes: bytes) -> str:
    """Base64-encode PDF bytes for the Anthropic Messages document block.

    Exposed as a separate helper so the orchestration code (worker /
    upload route) can attach the equipment PDF the same way the bank
    pipeline does in ``BedrockClient.extract_raw_json``. Kept here
    rather than inlined into ``extract_equipment_details`` so tests
    can swap in a stub client that ignores the bytes and returns a
    canned tool payload.
    """
    return base64.b64encode(pdf_bytes).decode("ascii")


__all__ = ["encode_pdf_for_bedrock", "extract_equipment_details"]
