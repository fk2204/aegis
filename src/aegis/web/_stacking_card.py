"""Stacking-card display helper.

Pulls the MCA-stacking summary the operator cares about out of an
``AnalysisRow`` and the document's classified transactions:

  * total daily MCA burden ($)
  * estimated active position count
  * monthly burden projection (daily * 22 business days)
  * MCA burden as a percentage of monthly revenue (debt-service drain)
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
from aegis.scoring_v2.lender_label import lender_label as _lender_label
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
    """Display payload for the merchant findings panel.

    ``mca_pct_of_deposits`` is the monthly MCA debt-service drain
    expressed as a percentage of monthly revenue — the operator-facing
    answer to "MCA drain is X% of revenue" instead of just "$Y/day."
    ``None`` when ``monthly_revenue`` is zero / missing (avoids a
    divide-by-zero and avoids rendering a meaningless 0.00%). Values
    above 100 are NOT capped: a burden that exceeds revenue is itself
    the underwriting signal and must surface unclipped.
    """

    daily_total: str
    monthly_burden: str
    position_count: int
    debit_count: int
    mca_pct_of_deposits: Decimal | None
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

    # Burden-as-percent-of-revenue. Use AnalysisRow.monthly_revenue
    # (true_revenue projected to 30 days by the parser, already filtered
    # of MCA proceeds where the LLM caught them) as the denominator.
    # Skip when revenue is zero/negative — a 0% reading there would be
    # actively misleading (the merchant has no income to compare to),
    # and the template suppresses the row when this field is None.
    monthly_revenue = analysis.monthly_revenue
    mca_pct_of_deposits: Decimal | None
    if monthly_revenue is None or monthly_revenue <= Decimal("0"):
        mca_pct_of_deposits = None
    else:
        mca_pct_of_deposits = (monthly / monthly_revenue * Decimal(100)).quantize(Decimal("0.01"))

    return StackingCard(
        daily_total=str(daily),
        monthly_burden=str(monthly),
        position_count=analysis.mca_positions,
        debit_count=len(debits),
        mca_pct_of_deposits=mca_pct_of_deposits,
        debits=debits,
    )


__all__ = ["StackingCard", "StackingDebit", "build_stacking_card"]
