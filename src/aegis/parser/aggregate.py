"""Deterministic aggregate computation over classified transactions.

Every metric returns its source transaction ids alongside its value. This
is the audit-trail requirement: the merchant detail page must be able to
answer "where did this number come from?" with specific PDF page/line refs.

Runs over the output of `classify.py` (the validated, classified list).
NEVER asks the LLM. Pure Python so it's deterministic and reviewable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from aegis.money import safe_divide
from aegis.parser.models import Aggregates, ClassifiedTransaction


@dataclass
class _Sourced:
    value: Decimal
    source_ids: list[UUID]


@dataclass
class _SourcedCount:
    value: int
    source_ids: list[UUID]


# Categories that count as revenue (deposits net of transfers and chargebacks).
_REVENUE_INCLUDED = frozenset({"deposit", "ach_credit", "wire_in", "refund"})
_REVENUE_EXCLUDED = frozenset({"transfer", "chargeback"})


def aggregate(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
    beginning_balance: Decimal,
) -> Aggregates:
    """Compute the canonical aggregates with full source attribution."""
    avg_dbl = _avg_daily_balance(transactions, beginning_balance, period_start, period_end)
    revenue = _true_revenue(transactions)
    nsf = _num_nsf(transactions)
    days_neg = _days_negative(transactions, beginning_balance, period_start, period_end)
    mca_daily = _mca_daily_total(transactions, period_start, period_end)
    debt_to_revenue = _debt_to_revenue(mca_daily.value, revenue.value)

    return Aggregates(
        avg_daily_balance={"value": avg_dbl.value, "source_ids": avg_dbl.source_ids},
        true_revenue={"value": revenue.value, "source_ids": revenue.source_ids},
        num_nsf={"value": nsf.value, "source_ids": nsf.source_ids},
        days_negative={"value": days_neg.value, "source_ids": days_neg.source_ids},
        debt_to_revenue=debt_to_revenue,
        mca_daily_total={"value": mca_daily.value, "source_ids": mca_daily.source_ids},
    )


# -- per-metric implementations ----------------------------------------------


def _avg_daily_balance(
    transactions: list[ClassifiedTransaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> _Sourced:
    """Time-weighted average of the running balance series.

    For days with multiple transactions, the closing running_balance is
    used. For days without any transaction, the previous day's closing
    balance carries forward. Source ids = every transaction whose
    running_balance contributed to the closing series.
    """
    if period_end < period_start:
        return _Sourced(value=Decimal("0.00"), source_ids=[])

    by_day: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for t in transactions:
        by_day[t.posted_date].append(t)

    sources: list[UUID] = []
    closing = beginning_balance
    total = Decimal("0")
    days = 0

    cursor = period_start
    while cursor <= period_end:
        rows = by_day.get(cursor, [])
        if rows:
            last_with_balance = next(
                (r for r in reversed(rows) if r.running_balance is not None), None
            )
            if last_with_balance is not None:
                closing = last_with_balance.running_balance  # type: ignore[assignment]
                sources.extend(r.id for r in rows)
            else:
                closing += sum((r.amount for r in rows), Decimal("0"))
                sources.extend(r.id for r in rows)
        total += closing
        days += 1
        cursor = _next_day(cursor)

    avg = safe_divide(total, Decimal(days)) if days else Decimal("0.00")
    return _Sourced(value=avg, source_ids=sources)


def _true_revenue(transactions: list[ClassifiedTransaction]) -> _Sourced:
    """Deposits net of transfers and chargebacks. Source ids = contributing rows."""
    total = Decimal("0")
    sources: list[UUID] = []
    for t in transactions:
        if t.category in _REVENUE_INCLUDED and t.amount > 0:
            total += t.amount
            sources.append(t.id)
        elif t.category in _REVENUE_EXCLUDED and t.amount > 0:
            # Subtract owner transfers / chargeback credits even though they
            # show as positive amounts — they're not real revenue.
            total -= t.amount
            sources.append(t.id)
    return _Sourced(value=total.quantize(Decimal("0.01")), source_ids=sources)


def _num_nsf(transactions: list[ClassifiedTransaction]) -> _SourcedCount:
    sources = [t.id for t in transactions if t.category == "nsf_fee"]
    return _SourcedCount(value=len(sources), source_ids=sources)


def _days_negative(
    transactions: list[ClassifiedTransaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> _SourcedCount:
    """Days where end-of-day running balance < 0.

    Source ids = transactions on each negative-balance day.
    """
    if period_end < period_start:
        return _SourcedCount(value=0, source_ids=[])

    by_day: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for t in transactions:
        by_day[t.posted_date].append(t)

    sources: list[UUID] = []
    closing = beginning_balance
    days_negative = 0

    cursor = period_start
    while cursor <= period_end:
        rows = by_day.get(cursor, [])
        if rows:
            last_with_balance = next(
                (r for r in reversed(rows) if r.running_balance is not None), None
            )
            if last_with_balance is not None:
                closing = last_with_balance.running_balance  # type: ignore[assignment]
            else:
                closing += sum((r.amount for r in rows), Decimal("0"))
        if closing < 0:
            days_negative += 1
            sources.extend(r.id for r in rows)
        cursor = _next_day(cursor)

    return _SourcedCount(value=days_negative, source_ids=sources)


def _mca_daily_total(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
) -> _Sourced:
    """Total MCA debit per day, averaged over the period.

    Source ids = every classified mca_debit row.
    """
    period_days = max(1, (period_end - period_start).days + 1)
    total = sum(
        (-t.amount for t in transactions if t.category == "mca_debit" and t.amount < 0),
        Decimal("0"),
    )
    sources = [t.id for t in transactions if t.category == "mca_debit"]
    avg_per_day = safe_divide(total, Decimal(period_days))
    return _Sourced(value=avg_per_day, source_ids=sources)


def _debt_to_revenue(mca_daily: Decimal, true_revenue: Decimal) -> Decimal:
    """MCA monthly burden / true_revenue. 22 trading days per month convention.

    Returns 0 when revenue is zero (avoids divide-by-zero; presence of MCA
    burden with zero revenue is caught by separate validators).
    """
    monthly_burden = mca_daily * Decimal(22)
    return safe_divide(monthly_burden, true_revenue)


def _next_day(d: date) -> date:
    from datetime import timedelta

    return d + timedelta(days=1)


__all__ = ["aggregate"]
