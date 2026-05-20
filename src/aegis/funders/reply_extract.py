"""Two-pass LLM extractor for funder-reply emails (mp Phase 10 / 2D-main).

Pass 1: Claude reads the raw email body and emits a permissive draft
JSON object (every field nullable so the LLM never has to invent values).

Pass 2: if the pass-1 output fails strict Pydantic validation we
re-prompt the LLM with the validation error, asking it to correct
itself. This is the "validation gate" inside the LLM round-trip —
distinct from the deterministic-math gate in ``replies.validate_reply``
which runs AFTER extraction on the structured terms.

The deterministic gate (amount * factor ≈ payback within $0.01) is NOT
done here. That stays in ``replies.validate_reply`` so the operator-paste
endpoint and the worker share one tie-out implementation.

All Claude calls go through ``LLMClient.classify_batch_json`` — text in,
JSON out. Production wiring uses ``BedrockClient`` with the regional
inference profile (us. prefix) per CLAUDE.md data-residency rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aegis.funders.replies import ReplyStatus, ReplyTerms
from aegis.funders.reply_prompts import (
    FUNDER_REPLY_EXTRACTION_PROMPT,
    FUNDER_REPLY_REPROMPT_PROMPT,
)
from aegis.llm import LLMClient
from aegis.logger import get_logger

_log = get_logger(__name__)


# Max raw-text length the worker forwards into Bedrock. Funder reply
# emails are short by nature; cap at 64 KB to bound token cost and
# refuse pathological inputs (someone pasting a 10MB thread).
_MAX_RAW_TEXT_BYTES: Final[int] = 64 * 1024

# Pass-2 is a single retry. If the LLM produces invalid JSON twice in a
# row, surface the failure to the caller — retrying again on the same
# prompt is unlikely to help and exhausts the cost budget.
_REPROMPT_BUDGET: Final[int] = 1


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


# Internal status the LLM may emit. We expose only the three statuses
# that the persistence layer accepts (approved/declined/countered) plus
# "unknown" for emails the model couldn't classify. Unknown is mapped
# to a fail-safe persistence path (no stamp; operator review) — not to
# countered, because countered has specific business semantics.
LLMReplyStatus = Literal["approved", "declined", "countered", "unknown"]


class FunderReplyExtractionDraft(BaseModel):
    """Pass-1 LLM output — permissive, all-nullable.

    The LLM is free to leave any field null; we never want the model
    to fabricate values to satisfy the schema. Strict types are still
    enforced (money fields must be strings parseable to Decimal,
    factor must be in [1.0, 2.0], etc.) — that's what the pass-2
    re-prompt catches.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: LLMReplyStatus
    decline_reason: str | None = Field(default=None, max_length=200)
    funder_name_text: str | None = Field(default=None, max_length=200)
    terms: FunderReplyTermsDraft = Field(default_factory=lambda: FunderReplyTermsDraft())
    parsed_confidence: int = Field(ge=0, le=100, default=0)
    notes: str | None = Field(default=None, max_length=2000)


class FunderReplyTermsDraft(BaseModel):
    """Pass-1 candidate offer terms. Money fields are string-typed so
    Pydantic refuses any float the model emits (binary float coercion
    is the gotcha CLAUDE.md flags). The conversion to Decimal happens
    in ``to_reply_terms`` after validation passes."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # Money fields are typed str — the JSON parser may give us
    # numbers, but we re-stringify before construction (see
    # _coerce_terms below) so Pydantic only ever sees strings.
    amount: str | None = None
    factor: str | None = None
    payback: str | None = None
    term_days: int | None = Field(default=None, ge=1, le=730)
    daily_payment: str | None = None
    holdback_pct: str | None = None


# Resolve forward ref now that FunderReplyTermsDraft is defined.
FunderReplyExtractionDraft.model_rebuild()


@dataclass
class FunderReplyExtractionResult:
    """Output of ``extract_funder_reply``.

    ``status`` is the funder-reply status the worker will pass to
    ``ingest_reply``. ``terms`` is the parsed structured offer; empty
    ``ReplyTerms()`` when the LLM didn't extract any. ``reprompted``
    is True iff pass 2 had to fire — a signal for monitoring how
    often the first pass produces invalid JSON.
    """

    status: ReplyStatus | Literal["unknown"]
    terms: ReplyTerms
    parsed_confidence: int
    decline_reason: str | None
    funder_name_text: str | None
    notes: str | None
    reprompted: bool


class FunderReplyExtractionError(RuntimeError):
    """Raised when both pass-1 and pass-2 LLM output fail validation,
    or when the LLM returns malformed JSON twice in a row.

    Callers (the worker) surface this as a job failure so the operator
    sees the inbound in the dashboard's error queue rather than the
    reply silently disappearing.
    """


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_funder_reply(
    raw_text: str,
    llm: LLMClient,
) -> FunderReplyExtractionResult:
    """Run the two-pass LLM extraction over a funder reply email body.

    Pass 1: emit the extraction prompt + raw email body, parse the
    JSON, validate as ``FunderReplyExtractionDraft``.

    Pass 2 (only on pass-1 validation failure): re-prompt with the
    validation errors + the prior output, parse + validate again.

    Returns the final ``FunderReplyExtractionResult`` ready for
    ``ingest_reply``. Raises ``FunderReplyExtractionError`` if both
    passes fail or the LLM returns malformed JSON twice.
    """
    if not raw_text or not raw_text.strip():
        raise FunderReplyExtractionError("empty raw_text")
    if len(raw_text.encode("utf-8")) > _MAX_RAW_TEXT_BYTES:
        raise FunderReplyExtractionError(
            f"raw_text exceeds {_MAX_RAW_TEXT_BYTES} bytes"
        )

    # Pass 1.
    pass1_prompt = FUNDER_REPLY_EXTRACTION_PROMPT + raw_text
    try:
        pass1_raw = llm.classify_batch_json(pass1_prompt)
    except ValueError as exc:
        # Malformed JSON on first attempt → re-prompt with the raw error.
        return _reprompt_or_fail(
            llm=llm,
            validation_errors=f"pass-1 JSON parse failure: {exc}",
            previous_output="<unparseable>",
            attempts_left=_REPROMPT_BUDGET,
        )

    try:
        draft = _validate_draft(pass1_raw)
    except ValidationError as exc:
        # Pass-1 produced JSON but it failed strict validation → pass 2.
        return _reprompt_or_fail(
            llm=llm,
            validation_errors=_format_validation_error(exc),
            previous_output=json.dumps(pass1_raw)[:4000],
            attempts_left=_REPROMPT_BUDGET,
        )

    return _draft_to_result(draft, reprompted=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reprompt_or_fail(
    *,
    llm: LLMClient,
    validation_errors: str,
    previous_output: str,
    attempts_left: int,
) -> FunderReplyExtractionResult:
    """Run pass 2 (re-prompt). Raises if the budget is exhausted or pass 2
    also fails — never recurses past the budget."""
    if attempts_left <= 0:
        raise FunderReplyExtractionError(
            f"LLM exhausted re-prompt budget; last errors: {validation_errors}"
        )

    prompt = FUNDER_REPLY_REPROMPT_PROMPT.format(
        validation_errors=validation_errors,
        previous_output=previous_output,
    )
    try:
        pass2_raw = llm.classify_batch_json(prompt)
    except ValueError as exc:
        raise FunderReplyExtractionError(
            f"LLM returned malformed JSON twice; last error: {exc}"
        ) from exc

    try:
        draft = _validate_draft(pass2_raw)
    except ValidationError as exc:
        raise FunderReplyExtractionError(
            f"LLM pass-2 still failed schema validation: {_format_validation_error(exc)}"
        ) from exc

    return _draft_to_result(draft, reprompted=True)


def _validate_draft(raw: dict[str, Any]) -> FunderReplyExtractionDraft:
    """Coerce + validate a raw JSON dict into the strict draft model.

    Coercion is minimal: stringify any numeric money values the LLM
    emitted (some models love bare numbers). After coercion the strict
    Pydantic model is the validator — float-typed amount fails here,
    which is what triggers pass 2.
    """
    coerced = dict(raw)
    if "terms" in coerced and isinstance(coerced["terms"], dict):
        coerced["terms"] = _coerce_terms(coerced["terms"])
    return FunderReplyExtractionDraft.model_validate(coerced)


def _coerce_terms(raw: dict[str, Any]) -> dict[str, Any]:
    """Stringify money/decimal fields. Int term_days passes through.

    The LLM is told to quote money strings in the prompt, but it
    sometimes ignores that. Stringifying here is what prevents float
    binary-coercion gotchas downstream (Decimal(1.10) -> 1.1000…0096).
    """
    out: dict[str, Any] = {}
    for key in ("amount", "factor", "payback", "daily_payment", "holdback_pct"):
        if key in raw and raw[key] is not None:
            value = raw[key]
            if isinstance(value, float):
                # Reject floats from the LLM by NOT stringifying — let
                # Pydantic fail validation so pass 2 fires with a clear
                # error. Float -> str loses precision; pass 2 is the
                # safer recourse.
                out[key] = value  # Pydantic will reject (str expected)
            elif isinstance(value, (int, str, Decimal)):
                out[key] = str(value)
            else:
                out[key] = value  # let pydantic reject
    if "term_days" in raw and raw["term_days"] is not None:
        out["term_days"] = raw["term_days"]
    return out


def _format_validation_error(exc: ValidationError) -> str:
    """Build a compact, LLM-friendly summary of a Pydantic ValidationError.

    Limits the result to ~2000 chars so the pass-2 prompt stays bounded.
    """
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []))
        msg = err.get("msg", "<unknown>")
        lines.append(f"- {loc}: {msg}")
        if sum(len(line) for line in lines) > 1900:
            lines.append("- ... (truncated)")
            break
    return "\n".join(lines) if lines else "<unspecified validation failure>"


def _draft_to_result(
    draft: FunderReplyExtractionDraft, *, reprompted: bool
) -> FunderReplyExtractionResult:
    """Convert the LLM draft into the worker-facing result.

    The status mapping is identity (LLM may return "unknown", which
    flows through; the worker decides whether to call ingest_reply or
    audit-and-drop).
    """
    return FunderReplyExtractionResult(
        status=draft.status,
        terms=_terms_to_reply_terms(draft.terms),
        parsed_confidence=draft.parsed_confidence,
        decline_reason=draft.decline_reason,
        funder_name_text=draft.funder_name_text,
        notes=draft.notes,
        reprompted=reprompted,
    )


def _terms_to_reply_terms(draft: FunderReplyTermsDraft) -> ReplyTerms:
    """Build a strict ``ReplyTerms`` from the permissive draft.

    Money/decimal strings are converted via ``Decimal(str(...))`` —
    never via float. Per-field conversion failures (un-parseable string,
    or value outside ReplyTerms' ge/le bounds) drop that single field
    rather than the whole terms object; the deterministic reconcile
    gate then catches the missing-fields case and lowers confidence
    appropriately. This keeps a partial parse usable rather than
    discarding amount + factor because holdback_pct was malformed.
    """
    decimal_inputs: list[tuple[str, str | None]] = [
        ("amount", draft.amount),
        ("factor", draft.factor),
        ("payback", draft.payback),
        ("daily_payment", draft.daily_payment),
        ("holdback_pct", draft.holdback_pct),
    ]
    kwargs: dict[str, Any] = {}
    for field_name, raw in decimal_inputs:
        if raw is None:
            continue
        parsed = _safe_decimal(raw)
        if parsed is None:
            # un-parseable string — skip this field, let downstream
            # math gate flag the missing data.
            continue
        kwargs[field_name] = parsed
    if draft.term_days is not None:
        kwargs["term_days"] = draft.term_days

    # Build progressively: drop any single field that fails ReplyTerms'
    # ge/le bounds (e.g. factor=Decimal("5.0") is out of [1, 2]) so the
    # rest of the parsed terms still surface.
    while True:
        try:
            return ReplyTerms(**kwargs)
        except ValidationError as exc:
            bad_field = _first_field_in_error(exc, set(kwargs.keys()))
            if bad_field is None:
                _log.warning(
                    "funder_reply.terms_conversion_dropped_all",
                )
                return ReplyTerms()
            _log.warning(
                "funder_reply.terms_field_dropped",
                extra={"field": bad_field},
            )
            kwargs.pop(bad_field, None)
            if not kwargs:
                return ReplyTerms()


def _first_field_in_error(
    exc: ValidationError, candidates: set[str]
) -> str | None:
    """Return the first ``loc[0]`` from a Pydantic error that names one
    of the known ReplyTerms fields. Used to drop bad fields one at a
    time so a single out-of-range value doesn't void the whole terms
    parse."""
    for err in exc.errors():
        loc = err.get("loc", ())
        if loc and str(loc[0]) in candidates:
            return str(loc[0])
    return None


def _safe_decimal(value: str) -> Decimal | None:
    """Best-effort Decimal conversion. Returns None on parse failure
    so the caller falls back to a None field rather than raising mid-
    conversion. Floats are NEVER constructed here."""
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


__all__ = [
    "FunderReplyExtractionDraft",
    "FunderReplyExtractionError",
    "FunderReplyExtractionResult",
    "FunderReplyTermsDraft",
    "LLMReplyStatus",
    "extract_funder_reply",
]
