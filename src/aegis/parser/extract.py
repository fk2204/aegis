"""Pass 1 — raw transaction extraction.

PDF -> Claude (Bedrock, document block) -> JSON -> Pydantic-validated
ExtractedStatement. NO classification here. NO aggregates here. The
downstream validation gate decides whether the document proceeds.

Per-page routing (mp Phase 6.5): when ``extract_statement_per_page``
is invoked, the PDF is sliced into contiguous same-strategy page
groups; each group is extracted with its preferred strategy (text or
vision), and the resulting transaction lists are unioned. The
``source_page`` on each transaction is remapped from the slice-local
page number back to the original 1-indexed page number so the audit
drill-down stays accurate.
"""

from __future__ import annotations

import io
from typing import Any, Final

import pikepdf
import pymupdf
from pydantic import ValidationError

from aegis.llm import LLMClient
from aegis.parser.models import ExtractedStatement, Transaction
from aegis.parser.page_router import PageStrategy, PageStrategyDecision
from aegis.parser.prompts import EXTRACTION_PROMPT, EXTRACTION_PROMPT_VISION

# Placeholder strings the LLM uses when a value isn't visible in the document.
# Compared case-insensitively, after .strip(). Coerced to None before
# Pydantic validation so:
#   * StatementSummary.account_last4 (max_length=4) doesn't fail validation
#     when the LLM emits "unknown" (7 chars) — the 2026-05-19 verify-bedrock
#     failure that surfaced this whole class of bug.
#   * StatementSummary.bank_name / account_holder don't get persisted as
#     the literal string "unknown" (silent corruption of bundling queries
#     and merchant-detail display).
#
# Any new Optional string field added to StatementSummary or another
# extraction model should be added to _coerce_summary (or a sibling
# coerce function) so it gets this protection. _coerce_optional_string
# is the single source of truth for the placeholder set.
_UNKNOWN_STRING_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "unknown",
        "n/a",
        "na",
        "none",
        "null",
        "tbd",
        "not available",
        "not visible",
        "not provided",
        "not specified",
        "see above",
        "-",
        "--",
    }
)


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

# Rasterization DPI for the OCR fallback. 200 DPI produces ~1700x2200 PNG
# for a US Letter page — enough resolution for Claude vision to read printed
# numbers and descriptions accurately, without ballooning the token cost.
_VISION_DPI: Final[int] = 200


def extract_statement(
    pdf_bytes: bytes,
    llm: LLMClient,
    *,
    prompt_suffix: str | None = None,
) -> ExtractionPass1Result:
    """Run pass 1 — extract raw transactions + printed summary.

    Parameters
    ----------
    pdf_bytes
        Raw PDF bytes. Caller is responsible for size and on-disk handling.
    llm
        An `LLMClient` (BedrockClient in production, fake in tests).
    prompt_suffix
        Optional verbatim text appended to the extraction system prompt.
        Used by the bank-layout-learning surface
        (``aegis.bank_layouts.BankLayoutRepository.get_hints``) to feed
        operator-curated layout hints into the prompt on subsequent
        parses of a bank we've seen before. ``None`` (default) keeps the
        base prompt unchanged.

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

    prompt = EXTRACTION_PROMPT + "\n\n" + prompt_suffix if prompt_suffix else EXTRACTION_PROMPT
    try:
        raw, truncated = llm.extract_raw_json(pdf_bytes, prompt)
    except ValueError as exc:
        raise ExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    if "summary" not in raw or "transactions" not in raw:
        raise ExtractionError(f"extraction JSON missing required keys; got {sorted(raw.keys())}")

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


def extract_statement_via_vision(
    pdf_bytes: bytes,
    llm: LLMClient,
    *,
    prompt_suffix: str | None = None,
) -> ExtractionPass1Result:
    """OCR fallback for image-only PDFs.

    Rasterises every page to PNG via pymupdf and runs extraction through
    the vision-pass LLM call. Output shape is identical to
    `extract_statement`, so the downstream validation gate runs unchanged.
    The pipeline branches on `metadata.has_text_layer`; this function is
    only invoked when the document has no extractable text layer.

    Same ``prompt_suffix`` semantics as ``extract_statement``: when
    provided, the operator-curated bank-layout hints are appended to
    the base vision prompt.
    """
    if len(pdf_bytes) == 0:
        raise ExtractionError("empty PDF buffer")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise ExtractionError(
            f"PDF buffer too large: {len(pdf_bytes)} bytes (max {_MAX_PDF_BYTES})"
        )

    page_images: list[bytes] = []
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
            if doc.page_count == 0:
                raise ExtractionError("PDF has zero pages")
            for i in range(doc.page_count):
                pix = doc.load_page(i).get_pixmap(dpi=_VISION_DPI)
                page_images.append(pix.tobytes("png"))
    except pymupdf.FileDataError as exc:
        raise ExtractionError(f"pymupdf could not open PDF: {exc}") from exc

    prompt = (
        EXTRACTION_PROMPT_VISION + "\n\n" + prompt_suffix
        if prompt_suffix
        else EXTRACTION_PROMPT_VISION
    )
    try:
        raw, truncated = llm.extract_raw_json_from_images(page_images, prompt)
    except ValueError as exc:
        raise ExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    if "summary" not in raw or "transactions" not in raw:
        raise ExtractionError(f"extraction JSON missing required keys; got {sorted(raw.keys())}")

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
    # ``beginning_balance`` and ``ending_balance`` are structurally required
    # by the schema, but some statements legitimately render them as null /
    # dash (account opened mid-period, $0 boundary, Brex-style summary
    # layouts). Coerce null → "0.00" so the row doesn't fail extraction;
    # the daily reconciliation gate in ``validate.py`` will still catch
    # downstream inconsistency between this 0 and the actual transaction
    # stream if the merchant really did have a non-zero balance — the
    # document just lands in manual_review instead of crashing the parse.
    for balance_key in ("beginning_balance", "ending_balance"):
        if out.get(balance_key) is None:
            out[balance_key] = "0.00"
    for k in ("beginning_balance", "ending_balance", "deposit_total", "withdrawal_total"):
        if k in out and out[k] is not None:
            out[k] = _num_to_str(out[k])
    if "withdrawal_total" in out and out["withdrawal_total"] is not None:
        # Statement summary withdrawal_total is conventionally printed as positive
        # but the parser model carries it as a Money. Take absolute value here so
        # negative-printed totals don't confuse downstream tie-out checks.
        out["withdrawal_total"] = _abs_str(out["withdrawal_total"])
    # Optional string fields on StatementSummary — LLMs sometimes emit
    # "unknown"/"N/A"/empty string instead of null when a value isn't
    # visible. account_last4 has max_length=4 so "unknown" was a hard
    # ValidationError; bank_name and account_holder are unconstrained
    # so "unknown" would land as a string in the database and corrupt
    # bundling queries / merchant-detail display. See
    # _UNKNOWN_STRING_PLACEHOLDERS at the top of this module for the
    # full normalization rule.
    for str_key in ("bank_name", "account_holder", "account_last4"):
        if str_key in out:
            out[str_key] = _coerce_optional_string(out[str_key])
    return out


def _coerce_optional_string(value: object) -> object:
    """Normalize LLM placeholder strings to ``None`` for Optional string fields.

    Empty strings, whitespace-only strings, and known placeholder tokens
    (case-insensitive after .strip(), see ``_UNKNOWN_STRING_PLACEHOLDERS``)
    are all coerced to ``None``. Legitimate values pass through with their
    surrounding whitespace stripped — Pydantic's str_strip_whitespace would
    do that downstream anyway, but doing it here keeps the membership check
    correct (so `" unknown "` is also normalized to None).

    Non-string inputs (already None, an int, etc.) pass through unchanged
    so this helper is idempotent and safe to apply to fields whose typing
    in the JSON payload is permissive.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.lower() in _UNKNOWN_STRING_PLACEHOLDERS:
        return None
    return stripped


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


# ---------------------------------------------------------------------------
# Per-page routing (mp Phase 6.5)
# ---------------------------------------------------------------------------


def extract_statement_per_page(
    pdf_bytes: bytes,
    llm: LLMClient,
    decisions: list[PageStrategyDecision],
    *,
    prompt_suffix: str | None = None,
) -> ExtractionPass1Result:
    """Extract the statement using per-page strategy routing.

    Splits the PDF into contiguous same-strategy page groups, runs the
    appropriate extractor on each group (text-extraction for text
    pages, vision for image pages), and unions the resulting
    transaction lists. ``source_page`` on each transaction is remapped
    from the slice-local page number back to the original 1-indexed
    page number so the audit trail stays accurate.

    The summary block is taken from the FIRST group's extraction —
    bank statements print the summary on the first page (cover) and
    that page's strategy is the one that read the printed totals.

    Pre-conditions enforced by the caller (``pipeline.run_pipeline``):
      - ``decisions`` length matches the PDF's page count.
      - ``has_low_confidence(decisions)`` is False (otherwise the
        pipeline routes the doc to manual_review without calling here).

    Raises ``ExtractionError`` on the same conditions as
    ``extract_statement`` / ``extract_statement_via_vision``.
    """
    if not decisions:
        raise ExtractionError("per-page extraction requires non-empty decisions list")
    if len(pdf_bytes) == 0:
        raise ExtractionError("empty PDF buffer")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise ExtractionError(
            f"PDF buffer too large: {len(pdf_bytes)} bytes (max {_MAX_PDF_BYTES})"
        )

    groups = _group_pages_by_strategy(decisions)
    if not groups:  # pragma: no cover — guarded by the `not decisions` check above
        raise ExtractionError("page grouping produced no groups")

    sub_results: list[tuple[ExtractionPass1Result, list[int]]] = []
    for strategy, page_indices in groups:
        slice_bytes = _slice_pdf(pdf_bytes, page_indices)
        if strategy == "text":
            result = extract_statement(slice_bytes, llm, prompt_suffix=prompt_suffix)
        else:
            result = extract_statement_via_vision(slice_bytes, llm, prompt_suffix=prompt_suffix)
        sub_results.append((result, page_indices))

    return _merge_sub_results(sub_results)


def _group_pages_by_strategy(
    decisions: list[PageStrategyDecision],
) -> list[tuple[PageStrategy, list[int]]]:
    """Walk decisions in order, batching consecutive same-strategy pages.

    Returns list of (strategy, [original_page_indices]) tuples in the
    order they appear in the source PDF. Each call to the LLM pays a
    per-request overhead, so grouping consecutive pages keeps the call
    count down on hybrid PDFs (e.g. 4 text pages + 2 vision pages = 2
    calls, not 6).
    """
    groups: list[tuple[PageStrategy, list[int]]] = []
    current_strategy: PageStrategy | None = None
    current_indices: list[int] = []
    for d in decisions:
        if d.strategy == current_strategy:
            current_indices.append(d.page_index)
        else:
            if current_strategy is not None:
                groups.append((current_strategy, current_indices))
            current_strategy = d.strategy
            current_indices = [d.page_index]
    if current_strategy is not None:
        groups.append((current_strategy, current_indices))
    return groups


def _slice_pdf(pdf_bytes: bytes, page_indices: list[int]) -> bytes:
    """Build a new PDF containing just ``page_indices`` (in given order).

    pikepdf's page-extraction creates a fresh PDF; the slice is
    self-contained (no shared references to the source) so it can be
    sent to Bedrock as its own document block.

    Raises ``ExtractionError`` on slicing failures so the pipeline can
    fail the document cleanly rather than crashing the worker.
    """
    if not page_indices:
        raise ExtractionError("cannot slice PDF with empty page index list")
    try:
        with pikepdf.open(io.BytesIO(pdf_bytes)) as src:
            new = pikepdf.Pdf.new()
            for idx in page_indices:
                if idx < 0 or idx >= len(src.pages):
                    raise ExtractionError(
                        f"page_router decision references out-of-range page "
                        f"{idx} (doc has {len(src.pages)} pages)"
                    )
                new.pages.append(src.pages[idx])
            buf = io.BytesIO()
            new.save(buf)
            return buf.getvalue()
    except pikepdf.PdfError as exc:
        raise ExtractionError(f"pikepdf slice failed: {exc}") from exc


def _merge_sub_results(
    sub_results: list[tuple[ExtractionPass1Result, list[int]]],
) -> ExtractionPass1Result:
    """Union the transaction lists, remap source_page, pick summary.

    Summary selection: the first group's summary is used. Bank
    statements print the summary on page 1, so whichever strategy
    handled the first group read the authoritative printed totals.
    The other groups' summaries are discarded (each sub-LLM-call
    re-parses 'the summary' on its slice and may invent totals from
    the visible rows — only the first slice's summary is trusted).

    truncated: True if ANY sub-call truncated. One truncated slice
    means the doc's transaction list is incomplete; the validation
    gate already routes truncated extractions to manual_review.

    synthetic_risk_indicators: union (dedup preserving first-seen
    order) so any indicator any strategy raised is surfaced.
    """
    if not sub_results:  # pragma: no cover — caller guarantees non-empty
        raise ExtractionError("merge requires at least one sub-result")

    first_summary = sub_results[0][0].statement.summary
    merged_txns: list[Transaction] = []
    indicators: list[str] = []
    seen_indicators: set[str] = set()
    any_truncated = False

    for result, original_page_indices in sub_results:
        any_truncated = any_truncated or result.truncated
        for indicator in result.synthetic_risk_indicators:
            if indicator not in seen_indicators:
                seen_indicators.add(indicator)
                indicators.append(indicator)
        merged_txns.extend(
            _remap_txn_source_pages(result.statement.transactions, original_page_indices)
        )

    merged_statement = ExtractedStatement(summary=first_summary, transactions=merged_txns)
    # The combined statement passes through the same source-attribution
    # check that the single-call paths run, in case a sub-call slipped
    # a bad row past its own validator (shouldn't, but defense in depth).
    _enforce_source_attribution(merged_statement)
    merged_statement = _renumber_duplicate_source_lines(merged_statement)
    return ExtractionPass1Result(
        statement=merged_statement,
        synthetic_risk_indicators=indicators,
        truncated=any_truncated,
    )


def _remap_txn_source_pages(
    transactions: list[Transaction], original_page_indices: list[int]
) -> list[Transaction]:
    """Translate slice-local source_page (1-indexed) back to original.

    The LLM returns transactions with ``source_page=N`` where N is the
    1-indexed page within the SUBMITTED slice. We need to map that to
    the 1-indexed page number in the ORIGINAL PDF.

    Example: slice contained original pages [0, 3, 5] (0-indexed) and
    the LLM returned source_page=2 for a transaction; that means the
    second page of the slice → original page 3 (0-indexed) → 4
    (1-indexed). Transactions with out-of-range source_page (LLM
    hallucinated a page beyond the slice) are kept but flagged: we
    don't drop them silently because the validator/aggregator may
    still produce a meaningful tie-out, and the operator should see
    the row in manual_review.
    """
    remapped: list[Transaction] = []
    for txn in transactions:
        local_page_1idx = txn.source_page
        if 1 <= local_page_1idx <= len(original_page_indices):
            original_0idx = original_page_indices[local_page_1idx - 1]
            new_source_page = original_0idx + 1
            remapped.append(txn.model_copy(update={"source_page": new_source_page}))
        else:
            # Out-of-range — preserve as-is so the validator's source
            # attribution check fires and routes the doc to
            # manual_review. Silent drop here would hide the bug.
            remapped.append(txn)
    return remapped


__all__ = [
    "ExtractionError",
    "ExtractionPass1Result",
    "extract_statement",
    "extract_statement_per_page",
    "extract_statement_via_vision",
]
