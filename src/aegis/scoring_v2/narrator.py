# ruff: noqa: E501
# The Bedrock system prompt below is a single multi-line string the model
# sees as-is; line breaks are part of the prompt structure, not source
# formatting, so the line-length rule is exempted at the file level.
"""Plain-English deal-summary narrator (Bedrock-driven).

The dossier surfaces score breakdowns, Track A/B verdicts, MCA stack
aggregates, fired-flag lists, funder match cards. Each is internally
consistent and individually auditable, but the operator still has to
mentally translate that raw signal into "should I submit this." This
module removes the translation layer: a single Bedrock call produces a
senior underwriter's three-section verbal handoff —

  1. ``deal_summary`` — 3-5 sentences, ALWAYS present.
  2. ``flag_explanations`` — one entry per fired flag with THIS deal's
     numbers (never a generic flag-dictionary definition).
  3. ``recommended_action`` — one of ``submit_now`` /
     ``call_first`` / ``request_documents`` / ``do_not_submit``, plus
     the exact next step.

Architecture notes:

* The model is called via ``aegis.llm.BedrockClient.invoke_tool_json``
  (added alongside this module). Centralized through the wrapper so the
  regional inference profile pin, retry budget, and future cost-account
  tracking apply uniformly.
* Output is a Pydantic model. The model_id and generated_at are part of
  the persisted shape so a stored summary is self-describing — an
  operator looking at last week's narrator output can tell whether it
  was regenerated under the current model or a stale one.
* On Bedrock failure or response validation failure the caller raises
  ``NarratorError``. The dossier route catches and renders an empty
  state; the refresh route surfaces a 503. The previously-good column
  value is NEVER overwritten by a failed call.

Per CLAUDE.md "Extraction & automation assists, never replaces
judgment": the narrator is a pre-fill assistant — it does NOT change
the live decline path, it does NOT mutate flags, it does NOT touch
``parse_status``. Track A integrity + Track B band continue to drive
the decision boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.scoring.models import ScoreResult
from aegis.scoring_v2.balance_health import BalanceHealthAggregation
from aegis.scoring_v2.mca_stack import MCAStackAggregation
from aegis.scoring_v2.track_a.models import IntegrityVerdict
from aegis.scoring_v2.track_b.models import BusinessRiskBand
from aegis.storage import AnalysisRow

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pydantic output models — what Bedrock fills via the forced tool call.

NarratorAction = Literal[
    "submit_now",
    "call_first",
    "request_documents",
    "do_not_submit",
]

NarratorSeverity = Literal["info", "warn", "decline"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class FlagExplanation(_StrictModel):
    """One fired flag with a plain-English, deal-specific explanation."""

    flag_code: str = Field(
        max_length=80,
        description=(
            "Short token identifying the flag — e.g. 'preloan_spike', "
            "'editor_detected:phantompdf', 'mca_stacking'. Mirrors the "
            "tokens already used elsewhere in the dossier so 'click a "
            "flag to drill in' lines up."
        ),
    )
    severity: NarratorSeverity
    explanation: str = Field(
        max_length=400,
        description=(
            "Plain-English sentence(s) citing THIS deal's actual numbers "
            "(e.g. '$12,500 unexplained deposit on 2026-04-03 — 7x the "
            "merchant's usual daily deposit'). Never a generic flag-"
            "dictionary definition."
        ),
    )


class RecommendedAction(_StrictModel):
    """The exact next thing to do — operator-actionable, not advisory."""

    action: NarratorAction
    next_step: str = Field(
        max_length=400,
        description=(
            "Exact next thing to do. If action == 'call_first', the "
            "EXACT question to ask the merchant. If 'request_documents', "
            "the named documents. If 'submit_now', the next click."
        ),
    )
    top_funder_match: str | None = Field(
        default=None,
        max_length=120,
        description="Funder name when one is the top match; None otherwise.",
    )
    estimated_terms: str | None = Field(
        default=None,
        max_length=160,
        description=(
            "Free-form terms hint when Track B + funder grid implies an "
            "offer — e.g. '1.35 factor, 6 months, $50k advance'. None "
            "when no offer is implied."
        ),
    )


# Schema version for the persisted JSON shape. Bump when the shape
# changes; the dossier render guards on this so a future schema
# change doesn't crash on legacy persisted rows.
NARRATOR_SCHEMA_VERSION: int = 1


class NarratorSummary(_StrictModel):
    """Output of ``narrate_deal``.

    Persisted to ``analyses.narrator_summary`` as JSONB. Read at every
    dossier render; refreshed in place by the operator via the
    "Refresh summary" button.
    """

    deal_summary: str = Field(
        max_length=2000,
        description="3-5 sentence verbal handoff — ALWAYS present.",
    )
    flag_explanations: tuple[FlagExplanation, ...] = Field(
        default_factory=tuple,
        description="One entry per fired flag; empty for clean deals.",
    )
    recommended_action: RecommendedAction
    model_id: str = Field(
        max_length=160,
        description="Exact Bedrock model id used (audit trail).",
    )
    generated_at: datetime
    version: int = Field(default=NARRATOR_SCHEMA_VERSION, ge=1)


class NarratorError(Exception):
    """Bedrock call or response validation failed.

    Callers catch and render an empty-state hint; the previously-good
    narrator_summary column is NEVER overwritten by a failed call.
    """


# ---------------------------------------------------------------------------
# Input shape — everything narrate_deal needs in one place.


@dataclass(frozen=True)
class NarratorContext:
    """Pre-built dossier context the narrator reads.

    Built by the caller from already-loaded objects (the dossier route
    has them; the refresh route assembles them from repository loads).
    Keeping the function pure-deterministic over this dataclass keeps
    tests straightforward — no repository wiring inside the narrator.

    Documentation-only counterparty fields default to None when the
    parser didn't surface them on this deal; the prompt includes them
    only when present.
    """

    merchant: MerchantRow
    document_id: UUID
    analysis: AnalysisRow
    score_result: ScoreResult
    track_a_verdict: IntegrityVerdict | None
    track_b_band: BusinessRiskBand | None
    mca_stack: MCAStackAggregation | None
    balance_health: BalanceHealthAggregation | None
    all_flags: tuple[str, ...]
    top_funder_name: str | None
    top_funder_factor: Decimal | None
    top_funder_advance: Decimal | None
    top_funder_term_days: int | None
    voided_check_on_file: bool
    drivers_license_on_file: bool
    bank_statements_months: int | None
    # Optional counterparty rollup hints derived from the parser
    # patterns; the prompt mentions these explicitly only when set so
    # the narrator doesn't fabricate B2B / food-service framing.
    counterparty_rollup: str | None = None


# ---------------------------------------------------------------------------
# Bedrock client protocol — tests inject a stub.


class _NarratorClient(Protocol):
    """Subset of BedrockClient the narrator uses.

    The production wrapper (``aegis.llm.BedrockClient``) implements
    ``invoke_tool_json`` with retry + the regional inference profile
    pin already baked in.
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
    ) -> tuple[dict[str, Any], str]:
        """Call Bedrock with a forced tool-use envelope.

        Returns ``(tool_input_json, model_id)`` where ``tool_input_json``
        is the model's structured output (must match ``tool_schema``)
        and ``model_id`` is the exact id the call used.
        """


# ---------------------------------------------------------------------------
# Prompt construction.

_SYSTEM_PROMPT = """You are writing a senior underwriter's verbal handoff for the operator at Commera Capital, an MCA broker. The operator already saw the raw numbers; your job is to translate them into "should I submit this."

You are writing for the operator, NOT for the merchant, NOT for the funder. Internal-only.

Hard rules:
1. Be direct. No hedging: ban "might", "could potentially", "may possibly", "appears to be". Say what's true.
2. No financial jargon unless industry-universal. MCA, ACH, factor rate, holdback are fine. GMV, ROAS, CAC, LTV are NOT — expand them inline.
3. NEVER invent numbers. Every quantitative claim must trace to a field in the structured context the user message gives you.
4. For each fired flag, cite THIS deal's actual numbers in the explanation, not a textbook definition. "$12,400 deposit on 2026-04-03, 7x the merchant's usual daily deposit" — not "preloan spike means an unusual large deposit before underwriting."
5. The recommended action is one of {submit_now, call_first, request_documents, do_not_submit}. Pick the most actionable. If call_first, write the EXACT question to ask the merchant.
6. deal_summary is 3-5 sentences. Cover: what the business is, the cashflow picture, the integrity picture, and what makes this deal interesting or risky. Use the actual monthly revenue figure from the context.
7. Output ONLY through the supplied tool. Do not write any conversational text outside the tool call.

Action selection logic (apply in order):
- Track A integrity verdict == "fail" → do_not_submit. next_step = "Decline — [integrity reason]."
- Missing voided_check or drivers_license at request_documents-eligible severity → request_documents. next_step lists the specific documents.
- Track B band == "high" OR multiple critical concerns → call_first. next_step = exact question (e.g. "Ask why the April deposit was 8x normal" or "Confirm the existing MCA daily payment").
- Track A integrity verdict in {clean} AND Track B band in {low, moderate} AND no missing docs → submit_now. next_step references the top funder match and the suggested terms.
- Track B band == "elevated" or any unresolved concern → call_first.
"""


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"${value:,.0f}"


def _pct(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.0%}"


def _build_user_payload(ctx: NarratorContext) -> dict[str, Any]:
    """Serialize the context into the JSON the user message carries.

    Pydantic ``mode='json'`` handles Decimal→str / UUID→str / date→str.
    Keys are stable so the prompt's "every claim must trace to a field"
    rule is enforceable on the operator side.
    """
    a = ctx.analysis
    merchant_block = {
        "business_name": ctx.merchant.business_name,
        "state": ctx.merchant.state,
        "industry": ctx.merchant.industry_choice or ctx.merchant.industry_naics,
        "time_in_business_months": ctx.merchant.time_in_business_months,
        "credit_score": ctx.merchant.credit_score,
    }
    cashflow_block = {
        "monthly_revenue": str(a.monthly_revenue),
        "true_revenue": str(a.true_revenue),
        "avg_daily_balance": str(a.avg_daily_balance),
        "lowest_balance": str(a.lowest_balance),
        "negative_days": a.days_negative,
        "nsf_count": a.num_nsf,
        "mca_positions": a.mca_positions,
        "mca_daily_total": str(a.mca_daily_total),
        "statement_period_start": a.statement_period_start.isoformat(),
        "statement_period_end": a.statement_period_end.isoformat(),
        "statement_days": a.statement_days,
    }
    score_block = {
        "score": ctx.score_result.score,
        "tier": ctx.score_result.tier,
        "recommendation": ctx.score_result.recommendation,
        "hard_decline_reasons": list(ctx.score_result.hard_decline_reasons),
        "soft_concerns": list(ctx.score_result.soft_concerns),
    }
    track_a_block: dict[str, Any] | None = None
    if ctx.track_a_verdict is not None:
        track_a_block = {
            "verdict": ctx.track_a_verdict.verdict,
            "branch": ctx.track_a_verdict.branch,
            "metadata_score": ctx.track_a_verdict.metadata_score,
            "rationale": ctx.track_a_verdict.rationale,
            "evidence": [
                {"signal": e.signal, "detail": e.detail} for e in ctx.track_a_verdict.evidence
            ],
        }
    track_b_block: dict[str, Any] | None = None
    if ctx.track_b_band is not None:
        track_b_block = {
            "band": ctx.track_b_band.band,
            "action": ctx.track_b_band.action,
            "reasons": [
                {
                    "factor": r.factor,
                    "severity": r.severity,
                    "detail": r.detail,
                }
                for r in ctx.track_b_band.reasons
            ],
            "insufficient_data_factors": list(ctx.track_b_band.insufficient_data_factors),
        }
    mca_block: dict[str, Any] | None = None
    if ctx.mca_stack is not None:
        mca_block = {
            "active_mca_count": ctx.mca_stack.active_mca_count,
            "estimated_combined_holdback_pct": (
                str(ctx.mca_stack.estimated_combined_holdback_pct)
                if ctx.mca_stack.estimated_combined_holdback_pct is not None
                else None
            ),
        }
    balance_block: dict[str, Any] | None = None
    if ctx.balance_health is not None:
        balance_block = {
            "avg_daily_balance": str(ctx.balance_health.avg_daily_balance),
            "negative_days": ctx.balance_health.negative_days,
        }
    funder_block: dict[str, Any] = {
        "top_funder_name": ctx.top_funder_name,
        "top_funder_factor": (
            str(ctx.top_funder_factor) if ctx.top_funder_factor is not None else None
        ),
        "top_funder_advance": (
            str(ctx.top_funder_advance) if ctx.top_funder_advance is not None else None
        ),
        "top_funder_term_days": ctx.top_funder_term_days,
    }
    checklist_block = {
        "voided_check_on_file": ctx.voided_check_on_file,
        "drivers_license_on_file": ctx.drivers_license_on_file,
        "bank_statements_months": ctx.bank_statements_months,
    }
    return {
        "merchant": merchant_block,
        "cashflow": cashflow_block,
        "score": score_block,
        "track_a": track_a_block,
        "track_b": track_b_block,
        "mca_stack": mca_block,
        "balance_health": balance_block,
        "fired_flags": list(ctx.all_flags),
        "funder_match": funder_block,
        "document_checklist": checklist_block,
        "counterparty_rollup": ctx.counterparty_rollup,
    }


# Tool schema. The model is forced to call this tool; ``tool_choice``
# guarantees the response is a single tool_use block whose input
# matches this schema.
_NARRATOR_TOOL_NAME = "emit_deal_summary"

_NARRATOR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deal_summary": {
            "type": "string",
            "description": (
                "3-5 sentences. Plain English. Cite the actual monthly "
                "revenue figure. No hedge words. ALWAYS present."
            ),
        },
        "flag_explanations": {
            "type": "array",
            "description": (
                "One entry per fired flag. Cite THIS deal's numbers, "
                "not a generic definition. May be empty for clean deals."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "flag_code": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warn", "decline"],
                    },
                    "explanation": {"type": "string"},
                },
                "required": ["flag_code", "severity", "explanation"],
            },
        },
        "recommended_action": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "submit_now",
                        "call_first",
                        "request_documents",
                        "do_not_submit",
                    ],
                },
                "next_step": {"type": "string"},
                "top_funder_match": {"type": ["string", "null"]},
                "estimated_terms": {"type": ["string", "null"]},
            },
            "required": ["action", "next_step"],
        },
    },
    "required": ["deal_summary", "flag_explanations", "recommended_action"],
}


# ---------------------------------------------------------------------------
# Public API.


def narrate_deal(
    ctx: NarratorContext,
    *,
    bedrock: _NarratorClient,
    max_tokens: int = 2000,
    temperature: float = 0.2,
) -> NarratorSummary:
    """Produce a plain-English deal summary via Bedrock tool-use.

    Raises ``NarratorError`` on Bedrock failure or response validation
    failure. Callers MUST catch this and decide whether to surface an
    empty-state hint (dossier render) or a 503 (refresh route). The
    previously-good ``analyses.narrator_summary`` column is NEVER
    overwritten by a failed call — the persistence helper guards on a
    non-None return.

    Parameters are deliberately split between the structural ``ctx``
    (everything the prompt sees) and the operational kwargs (``bedrock``,
    timing). Tests pass a stub for ``bedrock``; production passes the
    real ``BedrockClient``.
    """
    user_payload = _build_user_payload(ctx)
    user_prompt = (
        "Here is the structured context for the deal. Every quantitative "
        "claim in your output must trace to a field below. Emit the "
        "summary via the supplied tool — no other output.\n\n"
        + json.dumps(user_payload, sort_keys=True)
    )

    try:
        tool_input, model_id = bedrock.invoke_tool_json(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name=_NARRATOR_TOOL_NAME,
            tool_schema=_NARRATOR_TOOL_SCHEMA,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:  # narrow at the Bedrock client boundary
        _log.warning(
            "narrator_bedrock_failed",
            extra={"document_id": str(ctx.document_id), "error": str(exc)[:200]},
        )
        raise NarratorError(f"bedrock_call_failed: {exc}") from exc

    try:
        summary = NarratorSummary(
            deal_summary=str(tool_input["deal_summary"]),
            flag_explanations=tuple(
                FlagExplanation(**fe) for fe in tool_input.get("flag_explanations", [])
            ),
            recommended_action=RecommendedAction(**tool_input["recommended_action"]),
            model_id=model_id,
            generated_at=datetime.now(UTC),
            version=NARRATOR_SCHEMA_VERSION,
        )
    except Exception as exc:
        _log.warning(
            "narrator_response_invalid",
            extra={"document_id": str(ctx.document_id), "error": str(exc)[:200]},
        )
        raise NarratorError(f"narrator_response_validation_failed: {exc}") from exc

    return summary


__all__ = [
    "NARRATOR_SCHEMA_VERSION",
    "FlagExplanation",
    "NarratorAction",
    "NarratorContext",
    "NarratorError",
    "NarratorSeverity",
    "NarratorSummary",
    "RecommendedAction",
    "narrate_deal",
]
