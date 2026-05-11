"""Pass 1 — raw transaction extraction.

PDF -> Claude (Bedrock, document block) -> JSON -> Pydantic-validated
ExtractedStatement. NO classification here. NO aggregates here. The
downstream validation gate decides whether the document proceeds.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import ValidationError

from aegis.llm import LLMClient
from aegis.parser.models import ExtractedStatement
from aegis.parser.prompts import EXTRACTION_PROMPT


class ExtractionError(RuntimeError):
    """Raised when the LLM response cannot be parsed into ExtractedStatement."""


# Pull synthetic_risk_indicators into a dedicated structure so the validator
# can react to "INJECTION_ATTEMPT" / "PROCESSOR_HOLDBACK_SUSPECTED" without
# mixing them into the extraction's strict Pydantic shape.
class ExtractionPass1Result:
    """Container for pass 1 output (statement + advisory indicators).

    `truncated` is True when Bedrock cut the response off at max_tokens.
    Downstream `validate_extraction(...)` consumes this so a truncated
    response is surfaced as `extraction_truncated_retry_required` rather
    than getting misdiagnosed as a math reconciliation failure.
    """

    __slots__ = ("statement", "synthetic_risk_indicators", "truncated")

    def __init__(
        self,
        statement: ExtractedStatement,
        synthetic_risk_indicators: list[str],
        truncated: bool = False,
    ) -> None:
        self.statement = statement
        self.synthetic_risk_indicators = synthetic_risk_indicators
        self.truncated = truncated


# Defensive cap. Pass 1 should never need more than the document itself.
_MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024


def extract_statement(pdf_bytes: bytes, llm: LLMClient) -> ExtractionPass1Result:
    """Run pass 1 — extract raw transactions + printed summary.

    Parameters
    ----------
    pdf_bytes
        Raw PDF bytes. Caller is responsible for size and on-disk handling.
    llm
        An `LLMClient` (BedrockClient in production, fake in tests).

    Raises
    ------
    ExtractionError
        If the LLM response fails JSON parse, schema validation, or the
        required source-attribution fields are missing.
    """
    if len(pdf_bytes) == 0:
        raise ExtractionError("empty PDF buffer")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise ExtractionError(
            f"PDF buffer too large: {len(pdf_bytes)} bytes (max {_MAX_PDF_BYTES})"
        )

    try:
        raw, truncated = llm.extract_raw_json(pdf_bytes, EXTRACTION_PROMPT)
    except ValueError as exc:
        raise ExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    if "summary" not in raw or "transactions" not in raw:
        raise ExtractionError(
            f"extraction JSON missing required keys; got {sorted(raw.keys())}"
        )

    indicators = _coerce_indicators(raw.get("synthetic_risk_indicators", []))

    payload: dict[str, Any] = {
        "summary": _coerce_summary(raw["summary"]),
        "transactions": [_coerce_transaction(t) for t in raw["transactions"]],
    }

    try:
        statement = ExtractedStatement.model_validate(payload)
    except ValidationError as exc:
        raise ExtractionError(f"extraction payload failed schema validation: {exc}") from exc

    _enforce_source_attribution(statement)
    statement = _renumber_duplicate_source_lines(statement)

    return ExtractionPass1Result(
        statement=statement,
        synthetic_risk_indicators=indicators,
        truncated=truncated,
    )


def _coerce_indicators(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


def _coerce_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtractionError(f"summary must be an object, got {type(value).__name__}")
    # Numeric fields: convert to str so Pydantic Decimal coercion stays float-free.
    out: dict[str, Any] = dict(value)
    for k in ("beginning_balance", "ending_balance", "deposit_total", "withdrawal_total"):
        if k in out and out[k] is not None:
            out[k] = _num_to_str(out[k])
    if "withdrawal_total" in out and out["withdrawal_total"] is not None:
        # Statement summary withdrawal_total is conventionally printed as positive
        # but the parser model carries it as a Money. Take absolute value here so
        # negative-printed totals don't confuse downstream tie-out checks.
        out["withdrawal_total"] = _abs_str(out["withdrawal_total"])
    return out


def _coerce_transaction(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtractionError(f"transaction must be an object, got {type(value).__name__}")
    out: dict[str, Any] = dict(value)
    if "amount" in out and out["amount"] is not None:
        out["amount"] = _num_to_str(out["amount"])
    if out.get("running_balance") is not None:
        out["running_balance"] = _num_to_str(out["running_balance"])
    return out


def _num_to_str(value: object) -> str:
    """Convert any JSON number/string to a Decimal-safe string.

    Accepting a float here would silently lose precision (e.g. 0.10 == 0.1
    in float). We funnel through repr() for floats which preserves what the
    user actually got from the parser.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _abs_str(value: object) -> str:
    s = _num_to_str(value)
    return s.lstrip("-")


def _enforce_source_attribution(statement: ExtractedStatement) -> None:
    """Hard requirement: every transaction must carry source_page + source_line.

    Pydantic already enforces ge=1, but check explicitly with a sharper error
    so debugging "why is the audit trail empty?" is one read.
    """
    for i, txn in enumerate(statement.transactions):
        if txn.source_page < 1 or txn.source_line < 1:
            raise ExtractionError(
                f"transaction[{i}] missing source attribution: "
                f"page={txn.source_page} line={txn.source_line}"
            )


def _renumber_duplicate_source_lines(
    statement: ExtractedStatement,
) -> ExtractedStatement:
    """Deterministically renumber duplicate source_line values per page.

    Bedrock (Claude) sometimes returns the same ``source_line`` for two
    distinct transactions printed in a multi-column or side-by-side
    layout (real bank PDFs do this — Chase Business Checking, PNC
    eStatement). The audit-drill semantics expect unique (page, line)
    tuples so the operator can click a transaction and see exactly which
    printed row it came from.

    Strategy: walk transactions in input order; when we see a
    (page, line) tuple we've already seen, bump the line by 1 until we
    find an unused integer on that page. Preserves Claude's intended
    ordering; the displayed "page X line Y" remains a 1-indexed monotone
    integer per page. The original duplicate gets surfaced as a
    ``duplicate_source_line`` warning by the validator.
    """
    seen: dict[int, set[int]] = {}
    new_transactions = []
    for txn in statement.transactions:
        page = txn.source_page
        line = txn.source_line
        page_lines = seen.setdefault(page, set())
        while line in page_lines:
            line += 1
        page_lines.add(line)
        if line != txn.source_line:
            new_transactions.append(txn.model_copy(update={"source_line": line}))
        else:
            new_transactions.append(txn)
    return statement.model_copy(update={"transactions": new_transactions})


__all__ = ["ExtractionError", "ExtractionPass1Result", "extract_statement"]
