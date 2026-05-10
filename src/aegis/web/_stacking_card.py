"""Stacking-card display helper.

Pulls the MCA-stacking summary the operator cares about out of an
``AnalysisRow`` and the document's classified transactions:

  * total daily MCA burden ($)
  * estimated active position count
  * monthly burden projection (daily * 22 business days)
  * a flat list of contributing debits (lender label, amount, date)

When no MCA debits are detected the helper returns ``None`` so the
template can hide the card entirely.

The "lender label" is derived from the transaction description's first
token chunk — funder names show up at the front of the merchant
descriptor (``KAPITUS DEBIT 12345``, ``ON DECK ACH PMT``). Good enough
for an operator-facing summary; deeper attribution belongs in the
matcher, not this card.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from aegis.parser.models import ClassifiedTransaction
from aegis.storage import AnalysisRow

_BUSINESS_DAYS_PER_MONTH: Final[Decimal] = Decimal("22")


@dataclass(frozen=True)
class StackingDebit:
    """One MCA debit row in operator-readable form."""

    posted_date: str
    lender_label: str
    amount: str
    source_page: int
    source_line: int


@dataclass(frozen=True)
class StackingCard:
    """Display payload for the merchant findings panel."""

    daily_total: str
    monthly_burden: str
    position_count: int
    debit_count: int
    debits: list[StackingDebit]


def build_stacking_card(
    analysis: AnalysisRow,
    transactions: list[ClassifiedTransaction],
) -> StackingCard | None:
    """Return the card payload, or ``None`` if no stacking is detected.

    Detection rule mirrors how the rest of the app reads the analysis:
    if both ``mca_positions`` and ``mca_daily_total`` are zero, there's
    no stacking. We still surface a card if either is non-zero so a
    pattern detector flagging a single MCA shows up as "1 position".
    """
    daily = analysis.mca_daily_total
    if analysis.mca_positions == 0 and daily == Decimal("0"):
        return None

    debits = [
        StackingDebit(
            posted_date=t.posted_date.isoformat(),
            lender_label=_lender_label(t.description),
            amount=str(t.amount),
            source_page=t.source_page,
            source_line=t.source_line,
        )
        for t in sorted(
            (t for t in transactions if t.category == "mca_debit"),
            key=lambda t: (t.posted_date, t.source_page, t.source_line),
        )
    ]

    monthly = (daily * _BUSINESS_DAYS_PER_MONTH).quantize(Decimal("0.01"))

    return StackingCard(
        daily_total=str(daily),
        monthly_burden=str(monthly),
        position_count=analysis.mca_positions,
        debit_count=len(debits),
        debits=debits,
    )


def _lender_label(description: str) -> str:
    """Crude lender-name extractor.

    Takes up to the first 3 alphanumeric tokens. Uppercase tokens
    (``KAPITUS``, ``ONDECK``) are kept; mixed-case bank-noise tokens
    (``DEBIT``, ``Pmt``) are filtered out so the label stays readable.
    """
    cleaned = description.strip()
    if not cleaned:
        return "(unknown)"
    tokens = [tok for tok in cleaned.split() if tok.isalnum()]
    if not tokens:
        return cleaned[:32]
    keep: list[str] = []
    for tok in tokens[:5]:
        if tok.upper() in _NOISE_TOKENS:
            continue
        keep.append(tok)
        if len(keep) == 3:
            break
    return " ".join(keep) if keep else tokens[0][:32]


_NOISE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "DEBIT",
        "ACH",
        "PMT",
        "PAYMENT",
        "WITHDRAWAL",
        "DAILY",
        "TRANSFER",
        "INC",
        "LLC",
        "LTD",
        "CO",
    }
)


__all__ = ["StackingCard", "StackingDebit", "build_stacking_card"]
