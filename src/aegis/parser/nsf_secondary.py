"""Secondary validation for NSF (insufficient funds) classifications.

The LLM-driven classifier in ``parser/classify.py`` is the only authority
on whether a row is an ``nsf_fee``. A "FEE -50" descriptor missing the
literal "NSF" keyword can land in ``fee`` instead of ``nsf_fee``, and
vice versa — a benign maintenance fee on a positive-balance day can get
labeled NSF when descriptors contain ambiguous tokens. Today there is no
corroboration check.

This module adds a deterministic secondary pass that emits SHADOW flags
when classification confidence is low or when corroborating evidence
(negative running balance or co-located ACH return / reversal /
"INSUFFICIENT" descriptor) is absent. It does NOT change ``parse_status``
and it does NOT relabel rows — outputs are evidence flags only.

Per CLAUDE.md decision-boundary rule (shadow-first): the flag names
explicitly carry the ``would_route_review`` semantic so the operator
knows what the live routing would do after the shadow-mode flip. The
config flip itself happens elsewhere.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from aegis.parser.models import ClassifiedTransaction

# Tokens that, when present in a same-day or day-1 row's description,
# corroborate that an ACH return / reversal occurred. Case-insensitive
# substring match — the descriptor space for these events is small and
# the FP cost of a substring hit on a corroborating row is zero (we are
# only deciding whether to emit a shadow flag, not gating routing).
_RETURN_REVERSAL_TOKENS: Final[tuple[str, ...]] = (
    "RETURN",
    "REVERSAL",
    "INSUFFICIENT",
    "REVERSED",
    "RETURNED",
    "NSF",  # explicit on another row corroborates the NSF event itself
)

# Co-location window: a return/reversal on the SAME day or the day BEFORE
# the NSF fee is treated as corroboration. Same-day captures the bank
# posting the fee alongside the failed transaction; day-1 captures the
# common pattern where the return posts late afternoon and the NSF fee
# posts early next morning.
_COLOCATION_WINDOW_DAYS: Final[int] = 1

# Confidence below which an NSF classification is treated as low-confidence
# regardless of corroboration. This is intentionally MORE conservative than
# the pipeline's HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR (70) — we want to
# surface evidence on borderline rows even if they cleared the pipeline
# gate. 80 = "the LLM expressed reasonable certainty". Operator can tune
# after corpus signal.
_NSF_LOW_CONFIDENCE_THRESHOLD: Final[int] = 80

# Descriptor snippet length kept on the shadow flag — long enough to
# disambiguate similar rows on the same day without leaking PII at scale.
# Logger PII masking still applies upstream; this is for operator display.
_DESCRIPTION_SNIPPET_LEN: Final[int] = 30


@dataclass(frozen=True)
class NSFValidationIssue:
    """One shadow finding for an NSF row.

    ``flag_text`` is the wire format the pipeline appends to ``all_flags``.
    ``source_id`` and ``posted_date`` are kept for downstream audit drill-
    down. ``kind`` lets the operator dashboard group by issue type.
    """

    kind: str  # "corroboration_missing" or "low_confidence"
    source_id: str  # UUID stringified
    posted_date: date
    flag_text: str


def secondary_validate_nsf(
    classified_transactions: list[ClassifiedTransaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> list[NSFValidationIssue]:
    """Return shadow findings for NSF rows lacking corroboration or confidence.

    Two independent checks:

    1. **Corroboration missing** — for each ``nsf_fee`` row, the day's
       running balance must be negative OR another row on the same day or
       day-1 must be a ``chargeback`` OR carry a return/reversal token in
       its description. If neither holds, emit
       ``nsf_corroboration_missing:{date}_${amount}_{snippet}``.
    2. **Low confidence** — independently, for each ``nsf_fee`` row with
       ``classification_confidence`` < 80, emit
       ``nsf_low_confidence:{date}_${amount}_conf{N}_{snippet}``.

    A single row can fire BOTH checks (low-confidence AND uncorroborated)
    — they are independent signals. ``parse_status`` is not changed; the
    pipeline merely appends these to ``all_flags``.

    Args:
        classified_transactions: Output of ``classify_transactions``.
        beginning_balance: Statement opening balance — used as the seed
            for the running-balance carry-forward when running_balance
            is absent on rows.
        period_start: Inclusive start of the statement period.
        period_end: Inclusive end of the statement period.

    Returns:
        Empty list when no NSF rows exist or when every NSF row is
        corroborated AND high-confidence.
    """
    nsf_rows = [
        t for t in classified_transactions if t.category == "nsf_fee"
    ]
    if not nsf_rows:
        return []
    if period_end < period_start:
        return []

    negative_days = _compute_negative_days(
        classified_transactions, beginning_balance, period_start, period_end
    )
    by_day = _index_by_day(classified_transactions, period_start, period_end)

    issues: list[NSFValidationIssue] = []
    for nsf in nsf_rows:
        snippet = _description_snippet(nsf.description)
        amount_str = _format_amount(nsf.amount)
        date_str = nsf.posted_date.isoformat()

        corroborated = _is_corroborated(nsf, negative_days, by_day)
        if not corroborated:
            issues.append(
                NSFValidationIssue(
                    kind="corroboration_missing",
                    source_id=str(nsf.id),
                    posted_date=nsf.posted_date,
                    flag_text=(
                        f"nsf_corroboration_missing:{date_str}_"
                        f"${amount_str}_{snippet}_would_route_review"
                    ),
                )
            )

        if nsf.classification_confidence < _NSF_LOW_CONFIDENCE_THRESHOLD:
            issues.append(
                NSFValidationIssue(
                    kind="low_confidence",
                    source_id=str(nsf.id),
                    posted_date=nsf.posted_date,
                    flag_text=(
                        f"nsf_low_confidence:{date_str}_${amount_str}_"
                        f"conf{nsf.classification_confidence}_{snippet}_"
                        f"would_route_review"
                    ),
                )
            )

    return issues


def _is_corroborated(
    nsf: ClassifiedTransaction,
    negative_days: set[date],
    by_day: dict[date, list[ClassifiedTransaction]],
) -> bool:
    """An NSF row is corroborated when the day was in the red OR a return /
    reversal / chargeback co-located within the window.

    Same-day AND day-1 rows count as co-located. The window is intentionally
    tight; widening it risks false-positive corroboration from unrelated
    activity later in the period.
    """
    if nsf.posted_date in negative_days:
        return True

    window_start = nsf.posted_date - timedelta(days=_COLOCATION_WINDOW_DAYS)
    cursor = window_start
    while cursor <= nsf.posted_date:
        for row in by_day.get(cursor, []):
            if row.id == nsf.id:
                continue
            if row.category == "chargeback":
                return True
            if _has_return_reversal_token(row.description):
                return True
        cursor = cursor + timedelta(days=1)
    return False


def _has_return_reversal_token(description: str) -> bool:
    """Case-insensitive substring match against the return/reversal vocabulary."""
    if not description:
        return False
    upper = description.upper()
    return any(token in upper for token in _RETURN_REVERSAL_TOKENS)


def _compute_negative_days(
    transactions: list[ClassifiedTransaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> set[date]:
    """Mirror ``aggregate._nsf_negative_overlap_flag``'s balance derivation.

    Printed balance on the day's last row wins. Otherwise carry-forward
    signed amounts onto the running closing balance. Days with no rows
    inherit the prior day's closing. Returns the set of dates whose
    closing balance was strictly negative.
    """
    by_day: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for t in transactions:
        if period_start <= t.posted_date <= period_end:
            by_day[t.posted_date].append(t)

    negative_days: set[date] = set()
    closing = beginning_balance
    cursor = period_start
    while cursor <= period_end:
        rows = by_day.get(cursor, [])
        if rows:
            last_with_balance = next(
                (r for r in reversed(rows) if r.running_balance is not None),
                None,
            )
            if (
                last_with_balance is not None
                and last_with_balance.running_balance is not None
            ):
                closing = last_with_balance.running_balance
            else:
                closing = closing + sum(
                    (r.amount for r in rows), Decimal("0")
                )
        if closing < Decimal("0"):
            negative_days.add(cursor)
        cursor = cursor + timedelta(days=1)
    return negative_days


def _index_by_day(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> dict[date, list[ClassifiedTransaction]]:
    out: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for t in transactions:
        if period_start <= t.posted_date <= period_end:
            out[t.posted_date].append(t)
    return dict(out)


def _description_snippet(description: str) -> str:
    """Trim + collapse whitespace, cap length so flag text stays readable.

    Single-line, no commas (commas are the field separator in the flag's
    parent rendering layer).
    """
    if not description:
        return "no_description"
    cleaned = " ".join(description.split())
    if len(cleaned) > _DESCRIPTION_SNIPPET_LEN:
        cleaned = cleaned[:_DESCRIPTION_SNIPPET_LEN]
    return cleaned.replace(",", " ").replace("_", " ").strip() or "no_description"


def _format_amount(amount: Decimal) -> str:
    """Two-decimal absolute amount string for human-readable flag text."""
    return str(abs(amount).quantize(Decimal("0.01")))


__all__ = [
    "NSFValidationIssue",
    "secondary_validate_nsf",
]
