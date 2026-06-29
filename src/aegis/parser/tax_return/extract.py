"""Bedrock-vision tax-return extractor.

Detects the form type from filename + first-page text, then runs a
forced tool-use Bedrock call with a per-form schema and prompt. Returns
a validated ``TaxReturnExtraction`` carrying the figures the dossier
surfaces.

Architecture mirrors ``aegis.parser.equipment.extract``:

* Uses ``BedrockClient.invoke_tool_json`` (forced tool-use) so the
  model returns a single structured JSON blob — no prose, no partial
  output, no free-form fallback.
* ``temperature=0`` so re-running the extractor on the same return
  yields the same fields. Extraction is a deterministic pre-fill,
  not a generative task.
* Tool schema declares money figures as Decimal-safe strings. The
  sanitiser round-trips through ``Decimal`` and rejects garbage the
  same way ``aegis.parser.equipment.extract._coerce_money`` does.
* Tax year is integer 2000-2100 — anything outside the band is the
  model copying a date from elsewhere on the form (filing date,
  prior-year comparison column) and the row is dropped.

Per CLAUDE.md's "Extraction & automation assists, never replaces
judgment" rule, the row lands in ``tax_returns`` and surfaces on the
dossier; the operator confirms or edits via the merchant edit
surface. The extractor is PURE — it takes a PDF path + form type and
an LLM client and returns a model, never touching the repository.
"""

from __future__ import annotations

import base64
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import ValidationError

from aegis.logger import get_logger
from aegis.parser.tax_return.models import TaxFormType, TaxReturnExtraction

_log = get_logger(__name__)


# ----------------------------------------------------------------------
# Form detection — filename + first-page text.
# ----------------------------------------------------------------------

# Filename token → form type. Case-insensitive substring match on the
# basename. Order matters: 1120-S / 1120s MUST be checked before plain
# 1120 (the substring "1120" would otherwise win on every S-corp doc
# and the operator's S-corp returns would be misclassified as C-corp).
_FILENAME_FORM_PATTERNS: Final[tuple[tuple[str, TaxFormType], ...]] = (
    ("1120-s", "1120s"),
    ("1120s", "1120s"),
    ("form 1120-s", "1120s"),
    ("form 1120s", "1120s"),
    ("1065", "1065"),
    ("form 1065", "1065"),
    ("schedule c", "schedule_c"),
    ("schedule_c", "schedule_c"),
    ("schedule-c", "schedule_c"),
    # 1120 (C-corp) MUST come AFTER the 1120-S variants above so the
    # substring search doesn't shadow them.
    ("1120", "1120"),
    ("form 1120", "1120"),
)


# First-page-text marker → form type. These are the most distinctive
# phrases the IRS prints on the form's top-line / header — broad
# enough to survive OCR noise, narrow enough not to false-positive on
# correspondence about a return (which would say "your Form 1120" but
# rarely the structural heading "U.S. Corporation Income Tax Return").
# Case-insensitive substring check after lower-casing.
#
# Same ordering rule as the filename patterns: 1120-S markers come
# before 1120 so a return that prints both "Form 1120-S" and "Form
# 1120" in its header (some filing packages do this) routes to 1120s.
_TEXT_FORM_PATTERNS: Final[tuple[tuple[str, TaxFormType], ...]] = (
    ("u.s. income tax return for an s corporation", "1120s"),
    ("1120-s", "1120s"),
    ("1120s", "1120s"),
    ("u.s. return of partnership income", "1065"),
    ("form 1065", "1065"),
    ("1065", "1065"),
    ("profit or loss from business", "schedule_c"),
    ("schedule c (form 1040)", "schedule_c"),
    ("schedule c", "schedule_c"),
    ("u.s. corporation income tax return", "1120"),
    ("form 1120", "1120"),
    ("1120", "1120"),
)


def detect_tax_form_type(filename: str, first_page_text: str) -> TaxFormType | None:
    """Detect which business tax-return form the document is.

    Returns one of ``"1120" | "1120s" | "1065" | "schedule_c"`` or
    ``None`` when neither the filename nor the first-page text carries
    an unambiguous form marker.

    Filename check fires first — operators rename returns predictably
    ("2024 Acme Co Form 1120-S.pdf") and a filename hit is cheaper than
    parsing the page text. When the filename is generic
    ("scan_001.pdf"), falls through to the first-page-text check.

    PURE string match; no I/O. Callers pass the BASENAME (not the full
    path) — paths confuse the substring search ("c/users/jane 1120
    folder/return.pdf" would falsely match 1120).
    """
    if filename:
        name = filename.lower()
        for token, form in _FILENAME_FORM_PATTERNS:
            if token in name:
                return form
    if first_page_text:
        text = first_page_text.lower()
        for token, form in _TEXT_FORM_PATTERNS:
            if token in text:
                return form
    return None


# ----------------------------------------------------------------------
# Per-form Bedrock tool schemas + prompts.
# ----------------------------------------------------------------------


def _money_field(description: str) -> dict[str, Any]:
    """Tool-schema fragment for a Decimal-safe money string."""
    return {
        "type": "string",
        "description": description + " Decimal-safe string — no $, no comma, no k/M suffix. "
        'Example: "125000.00", not "$125,000". Omit if not stated.',
    }


_FORM_1120_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "tax_year": {
            "type": "integer",
            "description": (
                "Tax year the return covers (NOT the year it was filed). "
                "Printed on the form's top line as "
                '"For calendar year YYYY or tax year beginning ..." '
                "Four-digit integer 2000-2100."
            ),
        },
        "gross_receipts": _money_field("Form 1120 line 1a 'Gross receipts or sales'."),
        "net_income": _money_field("Form 1120 line 30 'Taxable income'."),
        "total_assets": _money_field("Form 1120 Schedule L line 15 'Total assets' end of year."),
        "total_liabilities": _money_field(
            "Form 1120 Schedule L line 21 'Total liabilities' end of year."
        ),
        "officer_compensation": _money_field(
            "Form 1120 line 12 'Compensation of officers' or Schedule E total."
        ),
    },
    "required": ["tax_year"],
    "additionalProperties": False,
}


_FORM_1120S_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "tax_year": {
            "type": "integer",
            "description": (
                "Tax year the return covers (NOT the year it was filed). "
                "Four-digit integer 2000-2100."
            ),
        },
        "gross_receipts": _money_field("Form 1120-S line 1a 'Gross receipts or sales'."),
        "net_income": _money_field("Form 1120-S line 21 'Ordinary business income (loss)'."),
        "total_assets": _money_field("Form 1120-S Schedule L line 15 'Total assets' end of year."),
        "shareholder_compensation": _money_field(
            "Form 1120-S line 7 'Compensation of officers' / shareholder W-2."
        ),
    },
    "required": ["tax_year"],
    "additionalProperties": False,
}


_FORM_1065_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "tax_year": {
            "type": "integer",
            "description": (
                "Tax year the return covers (NOT the year it was filed). "
                "Four-digit integer 2000-2100."
            ),
        },
        "gross_receipts": _money_field("Form 1065 line 1a 'Gross receipts or sales'."),
        "net_income": _money_field("Form 1065 line 22 'Ordinary business income (loss)'."),
        "total_assets": _money_field("Form 1065 Schedule L line 14 'Total assets' end of year."),
        "partner_distributions": _money_field(
            "Form 1065 Schedule K line 19a 'Distributions' (cash + property)."
        ),
    },
    "required": ["tax_year"],
    "additionalProperties": False,
}


_SCHEDULE_C_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "tax_year": {
            "type": "integer",
            "description": (
                "Tax year the return covers (NOT the year it was filed). "
                "Four-digit integer 2000-2100."
            ),
        },
        "gross_receipts": _money_field("Schedule C line 1 'Gross receipts or sales'."),
        "net_income": _money_field("Schedule C line 31 'Net profit or (loss)'."),
        "cogs": _money_field("Schedule C line 42 'Cost of goods sold'."),
        "total_expenses": _money_field("Schedule C line 28 'Total expenses'."),
    },
    "required": ["tax_year"],
    "additionalProperties": False,
}


_FORM_SCHEMAS: Final[dict[TaxFormType, dict[str, Any]]] = {
    "1120": _FORM_1120_SCHEMA,
    "1120s": _FORM_1120S_SCHEMA,
    "1065": _FORM_1065_SCHEMA,
    "schedule_c": _SCHEDULE_C_SCHEMA,
}


_FORM_TOOL_NAMES: Final[dict[TaxFormType, str]] = {
    "1120": "record_form_1120",
    "1120s": "record_form_1120s",
    "1065": "record_form_1065",
    "schedule_c": "record_schedule_c",
}


_FORM_HUMAN_NAMES: Final[dict[TaxFormType, str]] = {
    "1120": "Form 1120 (C-corporation income tax return)",
    "1120s": "Form 1120-S (S-corporation income tax return)",
    "1065": "Form 1065 (partnership return of income)",
    "schedule_c": "Schedule C (Form 1040 sole-proprietor profit or loss)",
}


_SYSTEM_PROMPT_TEMPLATE: Final[str] = (
    "You are extracting figures from a {form_human_name} PDF. Your job "
    "is to populate the `{tool_name}` tool with ONLY values the form "
    "explicitly states on the lines named in the schema descriptions.\n\n"
    "HARD RULES:\n"
    "  * NEVER invent a value. If the line is blank or printed as 0, "
    "OMIT the field entirely — do NOT emit '0' or '0.00'. The dossier "
    "renderer hides omitted fields; emitting zero falsely shows the "
    "operator a populated row.\n"
    "  * NEVER pull a number from a prior-year comparison column. Many "
    "IRS forms print 'Current year | Prior year' side by side; the "
    "extraction concerns the CURRENT year column only.\n"
    "  * Money figures are Decimal-safe strings — no $, no comma, no "
    'k/M suffix. "125000" or "125000.00", never "$125,000" or "125k".\n'
    "  * `tax_year` is the year of the RETURN (the period the figures "
    "describe), NOT the filing date. A return filed in March 2025 for "
    "the 2024 tax year has tax_year=2024.\n"
    "  * If the document is not actually a {form_human_name} (the "
    "filename routed here but the page is a cover letter, an unrelated "
    "IRS notice, etc.) emit tax_year=0 and omit every money field. The "
    "post-extraction sanitiser will drop the result."
)


_USER_PROMPT: Final[str] = (
    "Extract the figures from the attached tax-return PDF. Return ONLY "
    "the lines the form explicitly states. Omit every field that is "
    "blank or printed as zero on the current-year line."
)


# ----------------------------------------------------------------------
# LLM client Protocol — mirrors the equipment extractor approach so
# test stubs only need to implement the one method we call.
# ----------------------------------------------------------------------


class _TaxReturnLLMClient(Protocol):
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


def extract_tax_return(
    file_path: str,
    *,
    form_type: TaxFormType,
    llm_client: _TaxReturnLLMClient,
) -> TaxReturnExtraction | None:
    """Run the Bedrock vision extraction over a tax-return PDF.

    Returns a validated ``TaxReturnExtraction`` on success.
    Returns ``None`` when:

      * The PDF body is empty / unreadable,
      * The model emitted the "not actually this form" sentinel
        (tax_year == 0),
      * Pydantic validation fails (tax_year out of band, money parse
        failure on every field).

    Raises on Bedrock-side hard failures (DataResidencyError,
    non-retryable APIStatusError, etc.) — those are configuration bugs
    the caller should NOT swallow.

    ``llm_client`` is injected so tests can pass a stub. Production
    callers pass a ``BedrockClient`` (matching the wider Protocol that
    accepts ``pdf_bytes``).
    """
    path = Path(file_path)
    if not path.exists():
        _log.warning("tax_return_extractor.missing_file path=%s", file_path)
        return None
    pdf_bytes = path.read_bytes()
    if not pdf_bytes:
        return None

    schema = _FORM_SCHEMAS[form_type]
    tool_name = _FORM_TOOL_NAMES[form_type]
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        form_human_name=_FORM_HUMAN_NAMES[form_type],
        tool_name=tool_name,
    )

    raw, _model_id = llm_client.invoke_tool_json(
        system_prompt=system_prompt,
        user_prompt=_USER_PROMPT,
        tool_name=tool_name,
        tool_schema=schema,
        max_tokens=2048,
        temperature=0.0,
        pdf_bytes=pdf_bytes,
    )

    sanitised = _sanitise_extraction(raw, form_type=form_type)
    if sanitised is None:
        return None

    try:
        return TaxReturnExtraction(**sanitised)
    except ValidationError as exc:
        _log.info(
            "tax_return_extractor.validation_failed errors=%s",
            exc.errors()[:3],
        )
        return None


# ----------------------------------------------------------------------
# Internal: defensive sanitiser
# ----------------------------------------------------------------------


def _sanitise_extraction(raw: dict[str, Any], *, form_type: TaxFormType) -> dict[str, Any] | None:
    """Validate and coerce the raw tool-use payload.

    Returns a dict suitable for ``TaxReturnExtraction(**dict)`` or
    ``None`` when the result is non-recoverable.
    """
    if not isinstance(raw, dict):
        return None

    tax_year = _coerce_year(raw.get("tax_year"))
    if tax_year is None:
        return None

    out: dict[str, Any] = {"form_type": form_type, "tax_year": tax_year}
    # Walk every Money field the schema allows; ones that aren't on a
    # given form are simply absent from ``raw`` and skipped.
    for key in (
        "gross_receipts",
        "net_income",
        "total_assets",
        "total_liabilities",
        "officer_compensation",
        "shareholder_compensation",
        "partner_distributions",
        "cogs",
        "total_expenses",
    ):
        if key not in raw:
            continue
        value = _coerce_money(raw.get(key))
        if value is not None:
            out[key] = value
    return out


def _coerce_money(value: Any) -> Decimal | None:  # noqa: ANN401 — heterogeneous LLM payload
    """Coerce an LLM money output to a Decimal or None.

    Drops negative values — tax-return figures can be negative on a
    loss-year (net income line 30 on a 1120 can print as a parenthesised
    negative), but the dossier renders absolute figures with a
    direction arrow; the sanitiser drops negatives so a malformed
    "-$1,250" string from a hallucinated field doesn't poison the
    aggregate. The trade-off is a real loss-year net_income won't
    surface — acceptable because the operator sees the form itself
    and the dossier surface is a pre-fill, not the source of truth.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool subclasses int — strict-equality reject so the model
        # can't sneak a True/False through the money lane.
        return None
    candidate = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not candidate:
        return None
    # Strip parentheses (IRS "(1,234.56)" loss notation) before the
    # Decimal parse — Decimal would reject the parens otherwise.
    if candidate.startswith("(") and candidate.endswith(")"):
        candidate = candidate[1:-1]
    try:
        decimal_value = Decimal(candidate)
    except (InvalidOperation, ValueError):
        _log.info("tax_return_extractor.dropping_unparseable_money raw=%r", value)
        return None
    if decimal_value < 0:
        return None
    return decimal_value.quantize(Decimal("0.01"))


def _coerce_year(value: Any) -> int | None:  # noqa: ANN401 — heterogeneous LLM payload
    """Coerce a tax_year to int in [2000, 2100] or None.

    The "not a tax return" sentinel (tax_year=0) drops the row by
    returning None. A model emitting a year outside [2000, 2100] is
    almost certainly copying a non-year field (an EIN, a phone number,
    a filing-status code); drop rather than ship.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 2000 <= value <= 2100 else None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned.isdigit():
            return None
        as_int = int(cleaned)
        return as_int if 2000 <= as_int <= 2100 else None
    return None


# ----------------------------------------------------------------------
# Production helper — base64-encode PDF bytes for Bedrock attachment.
# ----------------------------------------------------------------------


def encode_pdf_for_bedrock(pdf_bytes: bytes) -> str:
    """Base64-encode PDF bytes for the Anthropic Messages document block.

    Exposed so orchestration code can attach the tax-return PDF the same
    way the bank pipeline does in ``BedrockClient.extract_raw_json``.
    """
    return base64.b64encode(pdf_bytes).decode("ascii")


__all__ = [
    "detect_tax_form_type",
    "encode_pdf_for_bedrock",
    "extract_tax_return",
]
