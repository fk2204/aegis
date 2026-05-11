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

from aegis.logger import get_logger
from aegis.money import safe_divide
from aegis.parser.models import Aggregates, ClassifiedTransaction

logger = get_logger(__name__)


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


@dataclass
class AggregateResult:
    """Output of ``aggregate``.

    ``aggregates`` is the canonical Pydantic model. ``flags`` carries
    operator-visible warnings from the aggregation step (today: ADB
    partial coverage when mode-mix skips days). Pipeline appends these
    flags to ``parse_result.all_flags`` so the merchant detail page
    surfaces them.
    """

    aggregates: Aggregates
    flags: list[str]


def aggregate(
    transactions: list[ClassifiedTransaction],
    period_start: date,
    period_end: date,
    beginning_balance: Decimal,
) -> AggregateResult:
    """Compute the canonical aggregates with full source attribution.

    Also reports ADB partial-coverage when printed-mode skips days the
    bank didn't print a closing balance for (mode-mix skip). Previously
    that skip was log-only and invisible to operators.
    """
    period_days = max(1, (period_end - period_start).days + 1)
    avg_dbl, adb_skipped_days = _avg_daily_balance(
        transactions, beginning_balance, period_start, period_end
    )
    revenue = _true_revenue(transactions)
    nsf = _num_nsf(transactions)
    days_neg = _days_negative(transactions, beginning_balance, period_start, period_end)
    mca_daily = _mca_daily_total(transactions, period_start, period_end)
    debt_to_revenue = _debt_to_revenue(mca_daily.value, revenue.value, period_days)

    aggregates = Aggregates(
        avg_daily_balance={"value": avg_dbl.value, "source_ids": avg_dbl.source_ids},
        true_revenue={"value": revenue.value, "source_ids": revenue.source_ids},
        num_nsf={"value": nsf.value, "source_ids": nsf.source_ids},
        days_negative={"value": days_neg.value, "source_ids": days_neg.source_ids},
        debt_to_revenue=debt_to_revenue,
        mca_daily_total={"value": mca_daily.value, "source_ids": mca_daily.source_ids},
    )
    flags: list[str] = []
    if adb_skipped_days > 0:
        flags.append(f"adb_partial_coverage:{adb_skipped_days}/{period_days}")
    return AggregateResult(aggregates=aggregates, flags=flags)


# -- per-metric implementations ----------------------------------------------


def _avg_daily_balance(
    transactions: list[ClassifiedTransaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> tuple[_Sourced, int]:
    """Time-weighted average of the running balance series.

    Mode is detected once at the start of the period:

    * **printed mode** — if ANY in-period transaction has ``running_balance``,
      we require the LAST row of every transaction-day to carry one. Days
      that fail this requirement are *skipped* (they do not contribute to
      the average) and a warning is logged so the operator can investigate
      partial balance printing. Carry-forward is NOT used in printed mode
      because mixing the two silently masks reconciliation drift.
    * **carry-forward mode** — if NO in-period transaction has a
      running_balance, the closing balance is computed by adding signed
      amounts to the previous day's close.

    Source ids = every transaction whose row contributed to the closing
    series.
    """
    if period_end < period_start:
        return _Sourced(value=Decimal("0.00"), source_ids=[]), 0

    by_day: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for t in transactions:
        if period_start <= t.posted_date <= period_end:
            by_day[t.posted_date].append(t)

    # Detect mode from in-period rows. Mixing modes silently masks drift,
    # so once any printed running_balance is present we commit to printed
    # mode and require it on every day's last row.
    printed_mode = any(
        r.running_balance is not None for rows in by_day.values() for r in rows
    )

    sources: list[UUID] = []
    closing = beginning_balance
    total = Decimal("0")
    days = 0
    skipped_days = 0

    cursor = period_start
    while cursor <= period_end:
        rows = by_day.get(cursor, [])
        if rows:
            if printed_mode:
                last_row = rows[-1]
                if last_row.running_balance is None:
                    # Mode-mix: printed balances exist elsewhere but this
                    # day's closing row has none. Skip rather than fall
                    # back to carry-forward, which would silently override
                    # a later printed value with accumulated drift.
                    logger.warning(
                        "avg_daily_balance: skipping %s — printed mode but "
                        "last row has no running_balance",
                        cursor.isoformat(),
                    )
                    skipped_days += 1
                    cursor = _next_day(cursor)
                    continue
                closing = last_row.running_balance
                sources.extend(r.id for r in rows)
            else:
                # Carry-forward mode: no printed balances anywhere in the
                # period, so we sum signed amounts onto yesterday's close.
                closing += sum((r.amount for r in rows), Decimal("0"))
                sources.extend(r.id for r in rows)
        total += closing
        days += 1
        cursor = _next_day(cursor)

    avg = safe_divide(total, Decimal(days)) if days else Decimal("0.00")
    return _Sourced(value=avg, source_ids=sources), skipped_days


def _true_revenue(transactions: list[ClassifiedTransaction]) -> _Sourced:
    """Deposits net of transfers and chargebacks. Source ids = contributing rows.

    Excluded categories (transfer, chargeback) are subtracted by their
    *absolute* amount regardless of sign. A chargeback posted as ``-$X``
    already debits the deposit history, so revenue must be reduced by
    ``$X`` either way — otherwise debit-side chargebacks are silently
    ignored.
    """
    total = Decimal("0")
    sources: list[UUID] = []
    for t in transactions:
        if t.category in _REVENUE_INCLUDED and t.amount > 0:
            total += t.amount
            sources.append(t.id)
        elif t.category in _REVENUE_EXCLUDED:
            # Subtract owner transfers / chargebacks regardless of sign:
            # a +$X transfer-credit and a -$X chargeback-debit both
            # represent non-revenue activity to remove from the deposit
            # stream.
            total -= abs(t.amount)
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

    Filters transactions to ``period_start <= posted_date <= period_end``
    to avoid silently inflating the daily average with stray rows from
    outside the period (an upstream-validator concern, but we are
    defensive here).

    Source ids = every classified mca_debit row inside the period.
    """
    period_days = max(1, (period_end - period_start).days + 1)
    in_period = [
        t for t in transactions if period_start <= t.posted_date <= period_end
    ]
    total = sum(
        (-t.amount for t in in_period if t.category == "mca_debit" and t.amount < 0),
        Decimal("0"),
    )
    sources = [t.id for t in in_period if t.category == "mca_debit"]
    avg_per_day = safe_divide(total, Decimal(period_days))
    return _Sourced(value=avg_per_day, source_ids=sources)


def _debt_to_revenue(
    mca_daily: Decimal, true_revenue: Decimal, period_days: int
) -> Decimal:
    """MCA monthly burden / monthly revenue.

    Both numerator and denominator are normalized to a 30-day month so
    the ratio is length-invariant: a 14-day statement and a 31-day
    statement of the same merchant must yield the same ratio. Without
    normalization the period revenue is biased low for short statements,
    inflating the ratio. 22 trading days/month is the MCA convention for
    the burden side; 30 calendar days/month for revenue.

    Returns 0 when revenue is zero (avoids divide-by-zero; presence of
    MCA burden with zero revenue is caught by separate validators).
    """
    monthly_burden = mca_daily * Decimal(22)
    revenue_monthly = safe_divide(true_revenue * Decimal(30), Decimal(period_days))
    return safe_divide(monthly_burden, revenue_monthly)


def _next_day(d: date) -> date:
    from datetime import timedelta

    return d + timedelta(days=1)


__all__ = ["aggregate"]
