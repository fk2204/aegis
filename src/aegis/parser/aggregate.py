"""Deterministic aggregate computation over classified transactions.

Every metric returns its source transaction ids alongside its value. This
is the audit-trail requirement: the merchant detail page must be able to
answer "where did this number come from?" with specific PDF page/line refs.

Runs over the output of `classify.py` (the validated, classified list).
NEVER asks the LLM. Pure Python so it's deterministic and reviewable.
"""

from __future__ import annotations

import statistics
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

    ``monthly_breakdown`` is a per-calendar-month roll-up of deposits,
    withdrawals, and avg_balance for this statement. Used by the
    findings layer to compute month-over-month deltas across a renewal
    merchant's stack of statements.
    """

    aggregates: Aggregates
    flags: list[str]
    monthly_breakdown: list[dict[str, str]]


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

    # New fraud-signal insights — surface without persisting (no DB change
    # this round). Each is None when not applicable.
    concentration_flag = _customer_concentration_flag(transactions)
    if concentration_flag is not None:
        flags.append(concentration_flag)
    payroll_flag = _payroll_cadence_flag(transactions, revenue.value)
    if payroll_flag is not None:
        flags.append(payroll_flag)
    nsf_overlap_flag = _nsf_negative_overlap_flag(
        transactions, beginning_balance, period_start, period_end
    )
    if nsf_overlap_flag is not None:
        flags.append(nsf_overlap_flag)

    monthly_breakdown = _monthly_breakdown(
        transactions, beginning_balance, period_start, period_end
    )

    return AggregateResult(
        aggregates=aggregates,
        flags=flags,
        monthly_breakdown=monthly_breakdown,
    )


def _monthly_breakdown(
    transactions: list[ClassifiedTransaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> list[dict[str, str]]:
    """Per-calendar-month roll-up: deposits, withdrawals, avg_balance.

    Stored as a list of dicts (Decimals as strings) so the result
    round-trips cleanly through jsonb without precision loss. Empty list
    when period has no transactions.

    Avg balance per month uses the same time-weighted approach as
    _avg_daily_balance: if any in-month row has running_balance, we
    track end-of-day; otherwise we carry-forward signed amounts.
    """
    if period_end < period_start:
        return []
    if not transactions:
        return []

    by_month: defaultdict[str, list[ClassifiedTransaction]] = defaultdict(list)
    for t in transactions:
        if period_start <= t.posted_date <= period_end:
            key = f"{t.posted_date.year:04d}-{t.posted_date.month:02d}"
            by_month[key].append(t)

    if not by_month:
        return []

    out: list[dict[str, str]] = []
    closing = beginning_balance
    for month in sorted(by_month.keys()):
        rows = by_month[month]
        deposits = sum((r.amount for r in rows if r.amount > 0), Decimal("0"))
        withdrawals = sum(
            (-r.amount for r in rows if r.amount < 0), Decimal("0")
        )
        # Avg balance approximation: time-weighted across the month's rows.
        # We accept the small inaccuracy of using end-of-row balances
        # without daily carry-forward — month-over-month deltas are
        # robust to that. Refine if real-deal signal demands precision.
        balances = [r.running_balance for r in rows if r.running_balance is not None]
        if balances:
            avg_balance = sum(balances, Decimal("0")) / Decimal(len(balances))
        else:
            # Carry-forward fallback: closing-balance-at-end-of-month.
            for r in rows:
                closing += r.amount
            avg_balance = closing
        out.append(
            {
                "month": month,
                "deposits": str(deposits.quantize(Decimal("0.01"))),
                "withdrawals": str(withdrawals.quantize(Decimal("0.01"))),
                "avg_balance": str(avg_balance.quantize(Decimal("0.01"))),
            }
        )
    return out


# Trailing punctuation we never want to leak into a payee bucket key or
# display label. ACH descriptors routinely arrive as "ACME PAYMENT, INC"
# or "PAYWARD INTERACTIVE,"; we want the comma-stripped form so two
# variants bucket together AND the operator never sees "(payward
# interactive,)" rendered on the chip.
_PAYEE_LABEL_TRAILING_PUNCT = " ,;:.-"


def _clean_payee_label(description: str) -> str:
    """Bucket key + display label for a deposit counterparty.

    Same string is used at the bucketing step AND at the format step so
    aggregate math and chip text always agree. The 20-char cap matches
    the soft-signals chip width; padding from the underlying ACH
    descriptor is trimmed (multi-space, wrapping parens, trailing
    punctuation) so "PAYWARD INTERACTIVE, INC" and "PAYWARD INTERACTIVE"
    bucket together as ``payward interactive``.
    """
    cleaned = description.strip().lower()[:30]
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip("()")
    # Cap to display width first so the rstrip catches whatever trailing
    # punctuation the cap revealed (the original 30-char slice could
    # leave a comma at position 19 that would survive without this).
    cleaned = cleaned[:20].rstrip(_PAYEE_LABEL_TRAILING_PUNCT)
    return cleaned


def _customer_concentration_flag(
    transactions: list[ClassifiedTransaction],
) -> str | None:
    """Top deposit counterparty as share of GROSS deposits.

    Numerator: the top payee's positive deposits in ``_REVENUE_INCLUDED``.
    Denominator: the sum of *all* positive deposits in those same
    categories — no chargeback / transfer subtraction.

    Earlier the denominator was ``true_revenue`` (net of transfers and
    chargebacks). The mismatch produced ratios above 100% on real deals
    — a single dominant payer plus modest reversals made the chip read
    "104% of revenue", which is nonsense at face value. Workers expect a
    0-100% concentration metric. ``true_revenue`` (net) is reserved for
    the financial math that genuinely wants net (debt-to-revenue, APR,
    payback days). Concentration is a ratio of *one stream of deposits
    to all streams of deposits* and is therefore a gross/gross ratio.

    Returns None when no positive deposits exist or fewer than 3
    distinct counterparties show up (concentration is meaningful only
    with some baseline of payers).
    """
    deposits = [
        t
        for t in transactions
        if t.amount > 0 and t.category in _REVENUE_INCLUDED
    ]
    if not deposits:
        return None
    gross_revenue = sum((t.amount for t in deposits), Decimal("0"))
    if gross_revenue <= 0:
        return None
    by_payee: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for t in deposits:
        by_payee[_clean_payee_label(t.description)] += t.amount
    if len(by_payee) < 3:
        return None
    top_payee, top_total = max(by_payee.items(), key=lambda kv: kv[1])
    share_pct = round((top_total / gross_revenue) * 100)
    return f"top_counterparty_concentration:{share_pct}%_({top_payee})"


def _payroll_cadence_flag(
    transactions: list[ClassifiedTransaction], true_revenue: Decimal
) -> str | None:
    """Detect payroll cadence + payroll as %-of-revenue.

    Returns None when no payroll rows exist. Cadence buckets:
      - weekly:        median spacing 6-8 days
      - biweekly:      median spacing 13-16 days
      - semimonthly:   median spacing 14-18 days
        (overlaps biweekly today; refine with month-aware logic in a
        later round if signal demands it)
      - monthly:       median spacing 27-32 days
      - irregular:     anything else
    """
    payroll_rows = sorted(
        (t for t in transactions if t.category == "payroll"),
        key=lambda t: t.posted_date,
    )
    if not payroll_rows:
        return None
    if len(payroll_rows) < 2:
        return "payroll_cadence:irregular_count_1"
    spacing = [
        (payroll_rows[i + 1].posted_date - payroll_rows[i].posted_date).days
        for i in range(len(payroll_rows) - 1)
    ]
    median_spacing = statistics.median(spacing)
    cadence: str
    if 6 <= median_spacing <= 8:
        cadence = "weekly"
    elif 13 <= median_spacing <= 16:
        cadence = "biweekly"
    elif 27 <= median_spacing <= 32:
        cadence = "monthly"
    else:
        cadence = "irregular"

    if true_revenue <= 0:
        return f"payroll_cadence:{cadence}"
    payroll_total = sum(
        (abs(t.amount) for t in payroll_rows), Decimal("0")
    )
    pct = round((payroll_total / true_revenue) * 100)
    return f"payroll_cadence:{cadence}_{pct}%_of_revenue"


def _nsf_negative_overlap_flag(
    transactions: list[ClassifiedTransaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> str | None:
    """NSF events on negative-balance days.

    Helps the operator distinguish "NSFs spread out across the period"
    (processing anomalies) from "NSFs cluster on the same days the
    account is in the red" (real cashflow stress). Returns None when no
    NSFs exist.
    """
    nsf_rows = [t for t in transactions if t.category == "nsf_fee"]
    if not nsf_rows:
        return None

    # Reuse the same logic as _days_negative to compute negative-balance days.
    if period_end < period_start:
        return None
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
            if last_with_balance is not None and last_with_balance.running_balance is not None:
                closing = last_with_balance.running_balance
            else:
                closing += sum((r.amount for r in rows), Decimal("0"))
        if closing < 0:
            negative_days.add(cursor)
        cursor = _next_day(cursor)

    overlap = sum(1 for t in nsf_rows if t.posted_date in negative_days)
    return f"nsf_on_negative_days:{overlap}_of_{len(nsf_rows)}"


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
    """Positive deposit stream minus positive transfer/chargeback credits.

    Revenue counts ONLY positive credits from ``_REVENUE_INCLUDED``. From
    ``_REVENUE_EXCLUDED`` (``transfer``, ``chargeback``) we subtract ONLY
    positive amounts:

      * ``+$X transfer`` — owner moved money INTO the account; looks like
        a deposit but isn't revenue. Subtract.
      * ``+$X chargeback`` — credit-side reversal of a prior deposit.
        Subtract so the original deposit doesn't outlive the reversal.
      * ``-$X transfer`` — owner moved money OUT of the account. Already
        absent from the positive revenue stream — do NOT subtract again.
      * ``-$X chargeback`` — debit-side chargeback fee. Same logic as
        above. The fee never inflated revenue; subtracting it would
        double-count as a revenue reduction.

    Prior implementation subtracted ``abs(t.amount)`` regardless of sign,
    which over-penalized any merchant with outbound intra-bank transfers.
    """
    total = Decimal("0")
    sources: list[UUID] = []
    for t in transactions:
        if t.category in _REVENUE_INCLUDED and t.amount > 0:
            total += t.amount
            sources.append(t.id)
        elif t.category in _REVENUE_EXCLUDED and t.amount > 0:
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
