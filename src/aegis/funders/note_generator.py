# ruff: noqa: E501
# The Bedrock system prompt below is a single multi-line string the model
# sees as-is; line breaks are part of the prompt structure, not source
# formatting, so the line-length rule is exempted at the file level.
"""Auto-generate a professional funder-submission note via Bedrock.

Section 8.5 of the AEGIS build plan calls for a one-click "draft a
funder note" affordance on the dossier so the underwriter doesn't have
to hand-type the 150-200 word professional summary that every funder
desk expects with a deal. ``generate_funder_note`` is the pure call
into Bedrock: it builds the prompt, forces a single-key tool-use
response, validates the length cap on the way back, and returns the
note text.

The route layer is the side-effect site. This module:

* NEVER touches the DB or audit-log directly.
* NEVER auto-submits the note — the operator MUST click "Submit to
  {funder}" on the dossier before any persistence happens. Per
  CLAUDE.md "Extraction & automation assists, never replaces judgment":
  Bedrock proposes, the operator confirms.
* NEVER writes the note to the merchant row or any cache.
* Mirrors the ``scoring_v2.narrator`` pattern verbatim — forced
  tool-use, temperature 0 for determinism, strict Pydantic validation
  on the return shape, banded exception type so the route can render an
  empty-state hint when Bedrock is down instead of 500ing.

All Claude calls go through ``aegis.llm.BedrockClient`` (regional
inference profile pinned in settings — see CLAUDE.md data-residency
rule).
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.funders.models import FunderRow
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.offer import OfferRecommendation
from aegis.scoring_v2.track_a.models import IntegrityVerdict
from aegis.storage import AnalysisRow

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic output model. Bedrock fills this via the forced tool call.

# 150-200 words ~= 1000-1300 characters; soft cap 1800 leaves headroom for
# the longest plausible compliant note. Hard floor 300 chars catches a
# pathological "OK" response the operator would have to rewrite anyway.
# These limits mirror the JSON schema below — the cap is enforced in both
# directions so a Bedrock overshoot doesn't sail past Pydantic validation.
_MIN_NOTE_CHARS: Final[int] = 300
_MAX_NOTE_CHARS: Final[int] = 1800


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class FunderNoteResponse(_StrictModel):
    """One-field structured output: the funder-note body Bedrock produced.

    The dossier renders this verbatim inside an editable ``<textarea>``;
    the operator can edit before clicking "Submit to {funder}". The
    response is NEVER persisted directly by this module — the route
    accepts the edited body off the form post.
    """

    note: Annotated[str, Field(min_length=_MIN_NOTE_CHARS, max_length=_MAX_NOTE_CHARS)]


class FunderNoteGenerationError(Exception):
    """Bedrock call or response validation failed.

    The route catches and surfaces an HTMX-friendly empty-state instead
    of a 500. The merchant's dossier state is NEVER mutated by a
    failed draft call — the operator can retry without rolling back
    anything.
    """


# ---------------------------------------------------------------------------
# Bedrock client protocol — tests inject a stub.


class _NoteClient(Protocol):
    """Subset of ``BedrockClient`` this module uses.

    Production wrapper (``aegis.llm.BedrockClient``) implements
    ``invoke_tool_json`` with retry + the regional inference profile
    pin already baked in. Same shape the narrator uses.
    """

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


# ---------------------------------------------------------------------------
# Prompt construction.

_SYSTEM_PROMPT = """You are a senior MCA underwriter at Commera Capital writing a professional funder submission note. The note goes to a funder rep at the named funder; it does NOT go to the merchant.

Tone:
1. Direct and factual. No hedge words. No marketing language. No sales-y framing.
2. NO em-dashes (—). Use commas, periods, or parentheses instead.
3. Professional, peer-to-peer. The reader is another senior credit professional.
4. Length: 150 to 200 words. The tool schema enforces a hard cap.

Structure (one note, NOT bulleted):
1. Open with strengths — what makes this deal worth a look. Cite specific numbers from the context (revenue, ADB, TIB).
2. Acknowledge concerns honestly — stack position, NSFs, integrity caveats. Do not omit them. Do not over-soften them either.
3. Close with the proposed structure — requested amount, recommended advance, factor / term / monthly payment as supplied by the offer.

Hard rules:
* NEVER invent numbers. Every quantitative claim must trace to a field in the structured context the user message gives you.
* NEVER write em-dashes (—). Replace with commas, periods, parentheses, or "and".
* NEVER add marketing language ("incredible opportunity", "strong fit", "world-class").
* NEVER address the merchant. The reader is the funder rep.
* Output ONLY through the supplied tool. Do not write any conversational text outside the tool call.
"""

_TOOL_NAME = "emit_funder_note"

_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "note": {
            "type": "string",
            "minLength": _MIN_NOTE_CHARS,
            "maxLength": _MAX_NOTE_CHARS,
            "description": (
                "Professional 150-200 word funder submission note. "
                "Strengths first, concerns acknowledged, deal structure "
                "proposed. No em-dashes. No marketing language. "
                f"Hard length cap {_MAX_NOTE_CHARS} characters."
            ),
        },
    },
    "required": ["note"],
}


def _fmt_money(value: object) -> str:
    """Render a money figure as ``$NN,NNN`` or ``Not pulled`` when None."""
    if value is None:
        return "Not pulled"
    try:
        return f"${int(value):,}"  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "Not pulled"


def _fmt_int(value: object) -> str:
    """Render an integer figure or ``Not pulled`` when None."""
    if value is None:
        return "Not pulled"
    try:
        return str(int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "Not pulled"


def _fmt_decimal(value: object) -> str:
    """Render a Decimal-typed offer field or ``n/a`` when None."""
    if value is None:
        return "n/a"
    return str(value)


def _integrity_word(verdict: IntegrityVerdict | None) -> str:
    """Project a Track A verdict onto a single descriptive token.

    Mirrors ``merchants.py::_integrity_verdict_word`` so the funder
    note and the operator-typed note share vocabulary.
    """
    if verdict is None:
        return "unverified"
    if verdict.verdict == "clean":
        return "clean"
    if verdict.verdict == "review":
        return "flagged for review"
    if verdict.verdict == "fail":
        return "flagged"
    return "unverified"


def _entity_type_label(entity_type: object) -> str:
    """Render a MerchantRow.entity_type literal for the prompt context.

    ``None`` collapses to the empty string so the parenthetical reads
    cleanly even when the operator hasn't set the entity type yet.
    """
    if entity_type is None:
        return ""
    return str(entity_type).upper()


def _build_user_prompt(
    *,
    merchant: MerchantRow,
    analysis: AnalysisRow,
    offer: OfferRecommendation,
    funder: FunderRow,
    track_a_verdict: IntegrityVerdict | None,
    stated_mca_balance: object = None,
    lender_list: str | None = None,
) -> str:
    """Build the structured-context user prompt Bedrock receives.

    Field names align with the user's 8.5 spec, with substitutions noted:
    - ``true_monthly_revenue`` <- AnalysisRow.true_revenue
    - ``avg_daily_balance``    <- AnalysisRow.avg_daily_balance
    - ``nsf_count_3mo``        <- AnalysisRow.num_nsf (period total —
      AEGIS does not separately track 3mo, the field reflects the
      analyzed bundle window)
    - ``stated_mca_positions`` <- AnalysisRow.mca_positions
    - ``stated_mca_balance``   <- optional kwarg; AEGIS does not carry
      a stated stack balance column today so the operator-supplied
      value (or None) flows through.
    - ``fico``                 <- MerchantRow.credit_score
    - ``document_integrity``   <- Track A verdict word
    - ``requested_advance``    <- MerchantRow.requested_amount
    - ``proposed_terms``       <- OfferRecommendation.recommended_amount,
      .interest_rate_apr / .term_months when present (per build-plan
      8.5 substitution: revenue_based offers lack factor_rate/term_months,
      so we fall back to the OfferRecommendation rationale string).
    """
    entity_label = _entity_type_label(merchant.entity_type)
    state_label = (merchant.state or "—").upper()
    business_header = f"{merchant.business_name} ({entity_label}, {state_label})".replace(" ()", "")

    # Offer-side fields. revenue_based offers populate only the core
    # recommended_amount + rationale; loan / equipment offers populate
    # interest_rate_apr / term_months / monthly_payment. The prompt
    # surfaces whichever set is populated so Bedrock has the right
    # vocabulary for the product.
    factor_or_apr = _fmt_decimal(offer.interest_rate_apr)
    term_or_rationale: str
    if offer.term_months is not None:
        term_or_rationale = f"{offer.term_months} months"
    else:
        term_or_rationale = offer.rationale or "n/a"

    lines = [
        "Generate a professional funder submission note for this deal.",
        "",
        f"Business: {business_header}",
        f"TIB: {_fmt_int(merchant.time_in_business_months)} months",
        f"True Monthly Revenue: {_fmt_money(analysis.true_revenue)}",
        f"Average Daily Balance: {_fmt_money(analysis.avg_daily_balance)}",
        f"NSF Count (period): {_fmt_int(analysis.num_nsf)}",
        (
            f"MCA Positions: {_fmt_int(analysis.mca_positions)} "
            f"({lender_list or 'lender list not captured'})"
        ),
        f"Outstanding Balance: {_fmt_money(stated_mca_balance)}",
        f"FICO: {_fmt_int(merchant.credit_score)}",
        f"Document Integrity: {_integrity_word(track_a_verdict)}",
        "",
        f"Requested: {_fmt_money(merchant.requested_amount)}",
        (
            f"Proposed Terms: {_fmt_money(offer.recommended_amount)} at {factor_or_apr}, "
            f"{term_or_rationale}"
        ),
        "",
        f"Funder: {funder.name}",
        "",
        "Write a 150-200 word funder note. Direct and factual. Highlight "
        "strengths first, acknowledge any concerns, propose the deal "
        "structure. Professional tone. NO em-dashes, NO marketing "
        "language, NO sales-y framing.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API.


def generate_funder_note(
    merchant: MerchantRow,
    analysis: AnalysisRow,
    offer: OfferRecommendation,
    funder: FunderRow,
    *,
    llm_client: _NoteClient,
    track_a_verdict: IntegrityVerdict | None = None,
    stated_mca_balance: object = None,
    lender_list: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> str:
    """Generate a 150-200 word professional funder submission note.

    Bedrock-driven, forced tool-use, temperature 0 for determinism.
    Returns the note text string only; persistence is the route's job.

    The optional ``track_a_verdict`` / ``stated_mca_balance`` /
    ``lender_list`` kwargs let the route surface integrity + stack
    context when available. ``track_a_verdict=None`` collapses to
    "unverified" in the prompt so the model knows the integrity column
    wasn't checked rather than fabricating one.

    Raises ``FunderNoteGenerationError`` on Bedrock failure or response
    validation failure. The caller MUST catch this and decide whether
    to render an empty state hint or return a 503; this function does
    NOT swallow the error so the audit row can capture the failure
    cause.
    """
    user_prompt = _build_user_prompt(
        merchant=merchant,
        analysis=analysis,
        offer=offer,
        funder=funder,
        track_a_verdict=track_a_verdict,
        stated_mca_balance=stated_mca_balance,
        lender_list=lender_list,
    )

    try:
        tool_input, _model_id = llm_client.invoke_tool_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name=_TOOL_NAME,
            tool_schema=_TOOL_SCHEMA,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:  # narrow at the Bedrock client boundary
        _log.warning(
            "funder_note_bedrock_failed merchant_id=%s funder_id=%s error=%s",
            merchant.id,
            funder.id,
            str(exc)[:200],
        )
        raise FunderNoteGenerationError(f"bedrock_call_failed: {exc}") from exc

    try:
        validated = FunderNoteResponse(note=str(tool_input["note"]))
    except Exception as exc:
        _log.warning(
            "funder_note_response_invalid merchant_id=%s funder_id=%s error=%s",
            merchant.id,
            funder.id,
            str(exc)[:200],
        )
        raise FunderNoteGenerationError(f"funder_note_response_validation_failed: {exc}") from exc

    return validated.note


__all__ = [
    "FunderNoteGenerationError",
    "FunderNoteResponse",
    "generate_funder_note",
]
