"""Plain-English deal summary card.

Produces the prominent card at the top of the dossier that the underwriter
team reads first. Rule-based (no LLM call): collapses the structured
score + MCA stack + balance health + web-presence + Close-context signals
into one headline + a short body + a flat ``flags`` list, plus a
``verdict`` of ``clean`` / ``review`` / ``decline`` that drives the
card's background colour.

The wording is deliberately funder-facing — short sentences, no AEGIS
internal jargon ("Track A" / "Track B" / "shadow_flags" never appear).
The output is shaped so a copy-paste into a Close note or a phone call
reads like something a person would write.

For the LLM-driven funder-submission narrative (separate concern, larger
prose), see ``generate_funder_narrative`` in the same module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from aegis.merchants.models import MerchantRow
from aegis.scoring.models import ScoreResult
from aegis.scoring_v2.balance_health import BalanceHealthAggregation
from aegis.scoring_v2.mca_stack import MCAStackAggregation

DealVerdict = Literal["clean", "review", "decline"]


@dataclass(frozen=True)
class CloseContext:
    """Subset of merchant Close-context fields the summary may quote.

    Constructed by the dossier route from the persisted merchant row;
    accepting it as a dataclass instead of reading off ``MerchantRow``
    directly keeps the summary function pure and testable (tests can
    inject any combination of fields without needing a valid merchant).
    """

    lead_description: str | None = None
    notes_summary: str | None = None
    call_transcripts: str | None = None


@dataclass(frozen=True)
class DealSummary:
    """Output of ``generate_deal_summary``.

    Rendered as a card at the top of the dossier:
      * ``verdict`` -> background colour (green/amber/red)
      * ``headline`` -> bold one-line summary
      * ``body`` -> 2-3 sentence supporting paragraph
      * ``flags`` -> short bullet list of real issues
    """

    verdict: DealVerdict
    headline: str
    body: str
    flags: tuple[str, ...] = field(default_factory=tuple)


# Soft-concern substrings that are scoring noise, not real issues the
# underwriter needs to see in the summary card. The fuller list still
# shows up below in the matching panels.
_NOISE_PREFIXES: tuple[str, ...] = (
    "soft_score_below_threshold",
    "apr_not_computable",
    "missing stip: ",  # already aggregated in stip section
    "funder_selective_appetite",
)


def _verdict_from_score(score: ScoreResult) -> DealVerdict:
    if score.recommendation == "decline":
        return "decline"
    if score.recommendation == "approve" and not score.soft_concerns:
        return "clean"
    return "review"


def _format_money(amount: Decimal | None) -> str:
    """Money for human reading. ``None`` -> ``"unknown"``."""
    if amount is None:
        return "unknown"
    return f"${amount:,.0f}"


def _format_months(months: int | None) -> str:
    if not months:
        return "unknown"
    if months >= 24:
        years = months // 12
        rem = months % 12
        if rem == 0:
            return f"{years} years"
        return f"{years} years {rem} months"
    return f"{months} months"


def _format_pct(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, Decimal):
        return f"{value:.0%}"
    return f"{value:.0%}"


def _key_strengths(
    *,
    score: ScoreResult,
    mca_stack: MCAStackAggregation,
    balance_health: BalanceHealthAggregation,
) -> list[str]:
    """Plain-English snippets describing what's positive about the deal."""
    out: list[str] = []
    if mca_stack.active_mca_count == 0:
        out.append("no existing MCAs")
    if balance_health.avg_daily_balance > Decimal("5000"):
        out.append(f"ADB {_format_money(balance_health.avg_daily_balance)}")
    if not score.hard_decline_reasons and score.score >= 70:
        out.append("clean integrity profile")
    return out


def _key_concerns(
    *,
    score: ScoreResult,
    mca_stack: MCAStackAggregation,
    balance_health: BalanceHealthAggregation,
) -> list[str]:
    """Plain-English snippets describing what's worrying about the deal."""
    out: list[str] = []
    if mca_stack.active_mca_count >= 3:
        out.append(f"{mca_stack.active_mca_count} existing MCA positions")
    elif mca_stack.active_mca_count == 2:
        out.append("two existing MCA positions")
    if (
        mca_stack.estimated_combined_holdback_pct is not None
        and mca_stack.estimated_combined_holdback_pct > Decimal("0.30")
    ):
        out.append(
            f"daily holdback {_format_pct(mca_stack.estimated_combined_holdback_pct)} of revenue"
        )
    if balance_health.negative_days >= 3:
        out.append(f"{balance_health.negative_days} negative-balance days")
    return out


def _context_phrase(close_context: CloseContext) -> str | None:
    """One short sentence pulling colour from Close notes / calls.

    Keeps the body grounded in actual operator-captured detail rather
    than reciting the same metrics in different words. Returns ``None``
    when there's nothing operator-curated to quote.
    """
    notes = (close_context.notes_summary or "").strip()
    calls = (close_context.call_transcripts or "").strip()
    desc = (close_context.lead_description or "").strip()

    if calls:
        first_sentence = calls.split(".")[0].strip()
        if first_sentence:
            return f"Call summary: {first_sentence[:200]}."
    if notes:
        first_sentence = notes.split(".")[0].strip()
        if first_sentence:
            return f"Recent Close note: {first_sentence[:200]}."
    if desc:
        return f"Close lead context: {desc[:200]}."
    return None


def _flags_for_card(
    *,
    score: ScoreResult,
    merchant: MerchantRow,
) -> tuple[str, ...]:
    """Promote the real issues to the card.

    Hard declines + non-noise soft concerns + web-presence risk flags.
    De-duped, capped at 6 so the card doesn't grow into a wall of text.
    """
    flags: list[str] = list(score.hard_decline_reasons)
    for concern in score.soft_concerns:
        if any(concern.startswith(prefix) for prefix in _NOISE_PREFIXES):
            continue
        flags.append(concern)
    for tag in merchant.web_presence_flags or []:
        flags.append(f"web presence: {tag}")
    seen: set[str] = set()
    unique: list[str] = []
    for f in flags:
        normalized = f.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return tuple(unique[:6])


def _headline(
    *,
    verdict: DealVerdict,
    strengths: list[str],
    concerns: list[str],
) -> str:
    """Build the bold one-line headline.

    ``decline`` -> lead with what's wrong.
    ``clean`` -> lead with what's strong.
    ``review`` -> name the dominant tension.
    """
    if verdict == "decline":
        top = concerns[0] if concerns else "fails minimum thresholds"
        return f"Decline — {top}."
    if verdict == "clean":
        if strengths:
            return f"Strong deal — {', '.join(strengths[:3])}."
        return "Strong deal — no major concerns surfaced."
    # review
    if concerns and strengths:
        return f"Needs review — {concerns[0]} despite {strengths[0]}."
    if concerns:
        return f"Needs review — {concerns[0]}."
    if strengths:
        return f"Needs review — {strengths[0]} with soft concerns."
    return "Needs review — mixed signals."


def _body(
    *,
    score: ScoreResult,
    mca_stack: MCAStackAggregation,
    balance_health: BalanceHealthAggregation,
    merchant: MerchantRow,
    close_context: CloseContext,
) -> str:
    """2-3 sentences of supporting context for the headline.

    Sentence 1: a snapshot of the deal economics (revenue, ADB, advance).
    Sentence 2: a snapshot of the MCA stack or the dominant concern.
    Sentence 3 (optional): quoted colour from Close notes / calls so the
                           summary reads like the team's understanding,
                           not just metrics regurgitated.
    """
    sentences: list[str] = []

    adb = balance_health.avg_daily_balance
    advance = score.suggested_max_advance or Decimal("0")
    tib = merchant.time_in_business_months
    s1 = (
        f"{merchant.business_name or 'Merchant'} shows {_format_money(adb)} ADB "
        f"with {_format_months(tib)} in business; "
        f"suggested advance {_format_money(advance)}."
    )
    sentences.append(s1)

    if mca_stack.active_mca_count == 0:
        sentences.append("No existing MCA positions found in the statements.")
    else:
        holdback = mca_stack.estimated_combined_holdback_pct
        if holdback is not None:
            sentences.append(
                f"{mca_stack.active_mca_count} existing MCA positions taking "
                f"{_format_pct(holdback)} daily holdback on revenue."
            )
        else:
            sentences.append(f"{mca_stack.active_mca_count} existing MCA positions detected.")

    phrase = _context_phrase(close_context)
    if phrase:
        sentences.append(phrase)

    return " ".join(sentences)


def generate_deal_summary(
    merchant: MerchantRow,
    score_result: ScoreResult,
    mca_stack: MCAStackAggregation,
    balance_health: BalanceHealthAggregation,
    close_context: CloseContext,
) -> DealSummary:
    """Collapse the structured signals into a plain-English summary card.

    Pure function; no LLM call. Tests can drive any combination of
    inputs deterministically. The dossier route is responsible for
    constructing each input from the persisted models.
    """
    verdict = _verdict_from_score(score_result)
    strengths = _key_strengths(
        score=score_result,
        mca_stack=mca_stack,
        balance_health=balance_health,
    )
    concerns = _key_concerns(
        score=score_result,
        mca_stack=mca_stack,
        balance_health=balance_health,
    )
    headline = _headline(verdict=verdict, strengths=strengths, concerns=concerns)
    body = _body(
        score=score_result,
        mca_stack=mca_stack,
        balance_health=balance_health,
        merchant=merchant,
        close_context=close_context,
    )
    flags = _flags_for_card(score=score_result, merchant=merchant)
    return DealSummary(verdict=verdict, headline=headline, body=body, flags=flags)


_NARRATIVE_CAP: int = 1500  # chars
_NARRATIVE_MAX_TOKENS: int = 512

_NARRATIVE_PROMPT_TEMPLATE = """\
You are an MCA underwriter presenting a deal to a funder. Write a
factual, concise 3-4 sentence narrative summarising this deal as if
you were dropping it into a Slack or email to a funder rep. Plain
business English. No marketing language. No bullet points. No code
fences. Just the paragraph.

Deal facts:
- Business: {business_name}
- Industry: {industry}
- Time in business: {tib}
- Avg monthly revenue (true): {true_revenue}
- ADB: {adb}
- Existing MCA positions: {mca_count}
- Combined daily holdback: {holdback}
- Suggested advance: {suggested_advance}
- Suggested factor: {suggested_factor}
- Key strengths: {strengths}
- Key concerns: {concerns}
- Operator context: {operator_context}
- Recent Close note: {close_note}
- Call summary: {call_summary}

Write the narrative now."""


class _BedrockTextClient(Protocol):
    def generate_text(self, prompt: str) -> str: ...


def _narrative_prompt(
    *,
    merchant: MerchantRow,
    score_result: ScoreResult,
    mca_stack: MCAStackAggregation,
    balance_health: BalanceHealthAggregation,
    offer: object | None,  # OfferRecommendation, kept loose to avoid the import cycle
    close_context: CloseContext,
) -> str:
    """Build the prompt string for ``generate_funder_narrative``."""
    strengths = _key_strengths(
        score=score_result, mca_stack=mca_stack, balance_health=balance_health
    )
    concerns = _key_concerns(score=score_result, mca_stack=mca_stack, balance_health=balance_health)
    suggested_advance = (
        getattr(offer, "recommended_amount", None)
        or score_result.suggested_max_advance
        or Decimal("0")
    )
    suggested_factor = (
        getattr(offer, "factor_rate", None) or score_result.recommended_factor_rate or Decimal("0")
    )
    holdback = mca_stack.estimated_combined_holdback_pct

    return _NARRATIVE_PROMPT_TEMPLATE.format(
        business_name=merchant.business_name or "Unknown",
        industry=merchant.industry_choice or "unspecified",
        tib=_format_months(merchant.time_in_business_months),
        true_revenue="see ADB",  # true_revenue isn't on BalanceHealthAggregation today
        adb=_format_money(balance_health.avg_daily_balance),
        mca_count=mca_stack.active_mca_count,
        holdback=_format_pct(holdback),
        suggested_advance=_format_money(suggested_advance),
        suggested_factor=f"{suggested_factor:.3f}" if suggested_factor else "not set",
        strengths=", ".join(strengths) if strengths else "none surfaced",
        concerns=", ".join(concerns) if concerns else "none surfaced",
        operator_context=(merchant.deal_context or "—")[:300],
        close_note=(close_context.notes_summary or "—")[:300],
        call_summary=(close_context.call_transcripts or "—")[:300],
    )


def generate_funder_narrative(
    merchant: MerchantRow,
    score_result: ScoreResult,
    mca_stack: MCAStackAggregation,
    balance_health: BalanceHealthAggregation,
    offer: object | None,
    close_context: CloseContext,
    *,
    client: _BedrockTextClient | None = None,
) -> str:
    """Generate a 3-4 sentence funder-facing narrative via Bedrock.

    Returns the empty string on any failure (Bedrock unavailable,
    network blip, empty business name). Callers must NOT block on the
    narrative — it's a paste-into-Close convenience, never a safety
    gate.

    ``client`` is injected for testability. Tests stub a small object
    with a ``generate_text(prompt) -> str`` method; production lazily
    constructs ``BedrockClient`` so import paths that never need the
    narrative don't fail on missing creds.
    """
    if not (merchant.business_name or "").strip():
        return ""

    prompt = _narrative_prompt(
        merchant=merchant,
        score_result=score_result,
        mca_stack=mca_stack,
        balance_health=balance_health,
        offer=offer,
        close_context=close_context,
    )

    if client is None:
        try:
            from aegis.ops.cost_tracking import build_cost_tracking_client

            client = build_cost_tracking_client(call_type="narrator")
        except Exception:
            return ""

    try:
        text = client.generate_text(prompt)
    except Exception:
        return ""

    cleaned = (text or "").strip()
    return cleaned[:_NARRATIVE_CAP]


__all__ = [
    "CloseContext",
    "DealSummary",
    "DealVerdict",
    "generate_deal_summary",
    "generate_funder_narrative",
]
