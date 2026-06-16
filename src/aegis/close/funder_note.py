"""Funder-submission note formatter — operator-triggered Close activity.

When the underwriter clicks "Submit to Funder" on a dossier, the deal
posts to the Close Lead's activity feed as a plain-text Note with every
number a funder rep would want at a glance: cashflow shape, existing
stack, AEGIS verdict, integrity verdict, suggested terms, top-3 funder
matches.

``format_funder_note`` is a pure function. It NEVER touches Close, the
DB, or audit. The route at
``POST /ui/merchants/{id}/submit-to-funder`` is the side-effect site;
this module just builds the string. Pure-function discipline lets the
note shape be tested in isolation and lets a future operator preview UI
call the same function without committing to a POST.

Output contract
---------------
* Plain text only. No markdown, no HTML, no rich-text. Close renders
  Note bodies as plain text in the activity feed.
* Hard cap at 1500 characters. Above the cap, the funder-readable
  rendering on the Close side starts truncating. The formatter
  guarantees ``len(out) <= 1500`` across every input permutation —
  trim from the bottom of the lowest-priority sections first, then
  ellipsize the last surviving line so the message stays parseable.
* Fields read tolerantly: every optional input may be ``None`` (no
  score result, no offer, etc.). Lines whose data source is ``None``
  drop out rather than rendering a blank "— : —" line.

Decision-boundary posture: this surface is operator-facing only. The
note is what the underwriter shows the funder rep; AEGIS does not
auto-decline based on note contents and does not parse the note back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from aegis.merchants.models import MerchantRow
from aegis.scoring.models import FunderMatch, ScoreResult
from aegis.scoring_v2.balance_health import BalanceHealthAggregation
from aegis.scoring_v2.industry import IndustryTier
from aegis.scoring_v2.mca_stack import MCAStackAggregation
from aegis.scoring_v2.offer import OfferRecommendation


@dataclass(frozen=True)
class RenewalContext:
    """Prior-funding context attached when the operator triggers a renewal.

    Populated by the ``POST /ui/merchants/{id}/prepare-renewal`` route
    from the merchant's most-recent approved
    ``funder_note_submissions`` row. When the merchant has no prior
    approved submission, the route passes ``renewal_context=None`` to
    ``format_funder_note`` so the renewal note still posts but without
    the prior-funding header — the audit row still carries the
    ``is_renewal=True`` flag so the operator can grep history.

    All three fields are required when this dataclass is supplied; the
    route is responsible for deriving ``months_since_funding`` from
    ``(now - original_funding_date).days // 30`` before constructing it.
    """

    original_funding_date: date
    original_amount: Decimal
    months_since_funding: int


MAX_NOTE_LENGTH: Final[int] = 1500
"""Hard cap on the produced string length. Funder-readable rendering
on the Close side stops being reliable past this length; the formatter
trims aggressively rather than producing a >1500 byte payload."""

_TOP_FUNDER_LIMIT: Final[int] = 3
_HARD_DECLINE_LIMIT: Final[int] = 4
_SOFT_CONCERN_LIMIT: Final[int] = 3
_ELLIPSIS: Final[str] = "…"


def format_funder_note(
    *,
    merchant: MerchantRow,
    score_result: ScoreResult | None,
    offer: OfferRecommendation | None,
    mca_stack: MCAStackAggregation | None,
    balance_health: BalanceHealthAggregation | None,
    industry_tier: IndustryTier | None,
    matched_funders: list[FunderMatch],
    months_of_statements: int | None = None,
    true_revenue_monthly: Decimal | None = None,
    integrity_verdict: str | None = None,
    num_nsf: int | None = None,
    days_negative: int | None = None,
    renewal_context: RenewalContext | None = None,
) -> str:
    """Build the plain-text Note body for a funder submission.

    Pure. Always returns a non-empty string. Output is guaranteed to be
    ``<= MAX_NOTE_LENGTH`` characters; sections at the bottom of the
    layout are dropped first when the budget would be exceeded.

    Section ordering (highest-priority first; trim from the bottom):

    1. Business identity (name / state / industry).
    2. Period + revenue headline.
    3. Cashflow: ADB, NSFs/month, neg days/month.
    4. Existing stack: MCA count + combined holdback %.
    5. AEGIS verdict: score / tier / integrity verdict.
    6. Suggested offer: advance / factor / holdback.
    7. Hard declines (if present).
    8. Soft concerns (if present).
    9. Top-3 funder matches with their qualifying tier.

    Parameters
    ----------
    merchant
        Business identity. ``state`` and ``industry_choice`` are
        optional on the row but the note still renders cleanly when
        absent.
    score_result
        AEGIS score envelope. ``None`` when scoring did not run (the
        merchant has no analyzed bundle yet); the score / tier /
        decline-reason / soft-concern lines drop in that case.
    offer
        Sized advance recommendation from ``compute_offer``. ``None``
        means the deal is too small or capacity is exhausted; the
        "Suggested" line drops.
    mca_stack
        Existing MCA-stack rollup. Always pass even when the merchant
        has no MCA — ``active_mca_count == 0`` renders as "Existing
        stack: none".
    balance_health
        Balance-health rollup. ``None`` is treated as "no balance data
        yet" — the cashflow line drops.
    industry_tier
        Operator-confirmed industry tier from ``industry_risk_tier``.
        ``None`` collapses to "industry: unspecified".
    matched_funders
        Already-ranked funder list (highest match_score first). Pass
        whatever ``match_funder`` returned for the active funders; the
        formatter slices the top ``_TOP_FUNDER_LIMIT`` and surfaces each
        funder's best qualifying TierMatch (``tier_matches`` filtered to
        ``qualifies=True``, first wins).
    months_of_statements
        How many statement-months the score covers. Reads cleanly from
        ``score_window["months_used"]`` at the call site.
    true_revenue_monthly
        Monthly true revenue (post-transfer / chargeback net). Sourced
        from ``AnalysisRow.monthly_revenue``. The "true revenue" line
        drops when ``None``.
    integrity_verdict
        One-word integrity readout — ``"clean"`` / ``"flagged"`` /
        ``"unverified"`` / similar. The route derives this from the
        Track A verdict + the document parse_status mix. ``None``
        collapses to "integrity: unverified".
    num_nsf
        Period NSF count from ``ScoreInput.num_nsf``. Already
        month-normalized by the multi-month score path. ``None``
        drops the NSF token from the cashflow line.
    days_negative
        Period negative-balance day count from
        ``ScoreInput.days_negative``. Already month-normalized by the
        multi-month score path. ``None`` drops the neg-days token.
    renewal_context
        When supplied, the note is prefixed with a two-line RENEWAL
        header followed by a ``---`` separator and a blank line.
        ``None`` (default) makes the output byte-identical to the
        pre-Sprint-7 layout. The renewal header is the highest-priority
        section: ``_enforce_length`` keeps it intact and trims from the
        bottom of the standard sections.
    """
    lines: list[str] = []

    # 1. Identity
    identity_parts: list[str] = [merchant.business_name]
    if merchant.state:
        identity_parts.append(merchant.state.upper())
    industry = merchant.industry_choice or "industry unspecified"
    identity_parts.append(industry)
    lines.append(" · ".join(identity_parts))

    # 2. Period + revenue
    if months_of_statements is not None and months_of_statements > 0:
        period_line = f"Statements: {months_of_statements} month"
        if months_of_statements != 1:
            period_line += "s"
        if true_revenue_monthly is not None:
            period_line += f"; true revenue ~${_fmt_int(true_revenue_monthly)}/mo"
        lines.append(period_line)
    elif true_revenue_monthly is not None:
        lines.append(f"True revenue ~${_fmt_int(true_revenue_monthly)}/mo")

    # 3. Cashflow
    if balance_health is not None:
        cashflow = f"ADB ${_fmt_int(balance_health.avg_daily_balance)}"
        if num_nsf is not None:
            cashflow += f"; NSFs {int(num_nsf)}/mo"
        if days_negative is not None:
            cashflow += f"; neg days {int(days_negative)}/mo"
        lines.append(cashflow)

    # 4. Existing stack
    if mca_stack is not None:
        if mca_stack.active_mca_count == 0:
            lines.append("Existing stack: none")
        else:
            stack_line = f"Existing stack: {mca_stack.active_mca_count} MCA"
            if mca_stack.active_mca_count != 1:
                stack_line += "s"
            if mca_stack.estimated_combined_holdback_pct is not None:
                stack_line += (
                    f", combined holdback ~{_fmt_pct(mca_stack.estimated_combined_holdback_pct)}%"
                )
            lines.append(stack_line)

    # 5. AEGIS verdict
    if score_result is not None:
        verdict_parts = [
            f"AEGIS {score_result.score}/100 (tier {score_result.tier})",
        ]
        industry_label = _industry_tier_label(industry_tier)
        if industry_label:
            verdict_parts.append(f"industry {industry_label}")
        verdict_parts.append(f"integrity {integrity_verdict or 'unverified'}")
        lines.append("; ".join(verdict_parts))
    elif industry_tier is not None or integrity_verdict is not None:
        bits: list[str] = []
        industry_label = _industry_tier_label(industry_tier)
        if industry_label:
            bits.append(f"industry {industry_label}")
        bits.append(f"integrity {integrity_verdict or 'unverified'}")
        lines.append("; ".join(bits))

    # 6. Suggested offer
    if offer is not None:
        suggested = f"Suggested: ${_fmt_int(offer.recommended_amount)} advance"
        if score_result is not None and score_result.recommended_factor_rate > 0:
            suggested += f" @ {score_result.recommended_factor_rate}x"
        suggested += f", {_fmt_pct(offer.holdback_pct * Decimal('100'))}% holdback"
        lines.append(suggested)

    # 7. Hard declines
    if score_result is not None and score_result.hard_decline_reasons:
        declines = score_result.hard_decline_reasons[:_HARD_DECLINE_LIMIT]
        lines.append("Declines: " + ", ".join(declines))

    # 8. Soft concerns
    if score_result is not None and score_result.soft_concerns:
        concerns = score_result.soft_concerns[:_SOFT_CONCERN_LIMIT]
        lines.append("Concerns: " + ", ".join(concerns))

    # 9. Top funder matches
    if matched_funders:
        funder_lines = _format_funder_matches(matched_funders)
        if funder_lines:
            lines.append("Top funders:")
            lines.extend(funder_lines)

    body = _enforce_length(lines)
    if renewal_context is None:
        return body
    return _prepend_renewal_header(body, renewal_context)


def _prepend_renewal_header(body: str, ctx: RenewalContext) -> str:
    """Prefix the renewal header to a finalized note body.

    Header shape (one historical-funding line + one months-since line +
    a ``---`` separator):

        RENEWAL — Previously funded YYYY-MM-DD for $NN,NNN
        K months since original funding

        ---

        <body>

    The header is the highest-priority section. To preserve the
    ``MAX_NOTE_LENGTH`` invariant, the body is trimmed line-by-line from
    the bottom until the combined output fits. The header itself is
    never truncated because it is short (under 100 characters in
    practice) — if the header alone exceeded the cap the route's
    inputs would be pathological.
    """
    header = (
        f"RENEWAL — Previously funded {ctx.original_funding_date.isoformat()} "
        f"for ${_fmt_int(ctx.original_amount)}\n"
        f"{ctx.months_since_funding} months since original funding\n"
        f"\n"
        f"---\n"
        f"\n"
    )
    combined = header + body
    if len(combined) <= MAX_NOTE_LENGTH:
        return combined

    # Trim from the bottom of the body to fit. Body lines are already
    # length-enforced; we only need to drop trailing lines until the
    # header + remaining body fits.
    body_lines = body.split("\n")
    while body_lines:
        body_lines.pop()
        trimmed_body = "\n".join(body_lines)
        candidate = header + trimmed_body
        if len(candidate) <= MAX_NOTE_LENGTH:
            return candidate
    # Body empty + header > cap is the pathological case; ellipsize.
    if len(header) <= MAX_NOTE_LENGTH:
        return header
    return header[: MAX_NOTE_LENGTH - 1] + _ELLIPSIS


def _format_funder_matches(matched_funders: list[FunderMatch]) -> list[str]:
    """Render the top-N funders as plain-text lines.

    Each line: ``"  <name> [<score>] <qualifying-tier-or-no-tier>"``.
    Tier resolution: first ``TierMatch`` with ``qualifies=True`` wins.
    Funders without ``tier_matches`` (legacy / pre-extraction) get a
    blank tier slot.
    """
    out: list[str] = []
    for fm in matched_funders[:_TOP_FUNDER_LIMIT]:
        qualifying = next((t for t in fm.tier_matches if t.qualifies), None)
        if qualifying is not None:
            tier_label = qualifying.tier_name
        elif fm.tier_matches:
            tier_label = "no qualifying tier"
        else:
            tier_label = "tier n/a"
        out.append(f"  {fm.funder_name} [{fm.match_score}] {tier_label}")
    return out


def _industry_tier_label(tier: IndustryTier | None) -> str | None:
    """Render an IndustryTier as a one-word note token. Returns ``None``
    when the tier was not supplied; the caller drops the bit."""
    if tier is None:
        return None
    return tier.replace("_", " ")


def _fmt_int(value: Decimal) -> str:
    """Money formatted as a comma-separated integer dollar amount."""
    return f"{int(value):,}"


def _fmt_pct(value: Decimal) -> str:
    """Percent figures formatted to one decimal, trailing ``.0`` dropped."""
    quantized = value.quantize(Decimal("0.1"))
    text = format(quantized, "f")
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _enforce_length(lines: list[str]) -> str:
    """Assemble ``lines`` into a single string, dropping trailing lines
    until the total fits under ``MAX_NOTE_LENGTH``.

    Section order in ``lines`` is highest-priority first; the lowest-
    priority sections (matched funders, soft concerns, etc.) sit at the
    bottom and drop first. If even the first line exceeds the budget —
    pathological merchant name with no other sections — the line is
    truncated with an ellipsis. The function never returns a string
    longer than ``MAX_NOTE_LENGTH``.
    """
    working = list(lines)
    while working:
        text = "\n".join(working)
        if len(text) <= MAX_NOTE_LENGTH:
            return text
        working.pop()

    # All sections dropped — pathological. Truncate the first line.
    if not lines:
        return ""
    first = lines[0]
    if len(first) <= MAX_NOTE_LENGTH:
        return first
    return first[: MAX_NOTE_LENGTH - 1] + _ELLIPSIS


__all__ = [
    "MAX_NOTE_LENGTH",
    "RenewalContext",
    "format_funder_note",
]
