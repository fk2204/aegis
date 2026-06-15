"""Balance-health metrics — first-class output for dossier chips.

Reads the merchant's classified transactions and produces a small
rollup of cash-balance signals the underwriter sees alongside the
existing fraud / cashflow / MCA-stack chips:

* ``avg_daily_balance`` — time-weighted closing balance across the
  period. Mirrors ``parser.aggregate._avg_daily_balance``'s
  printed-mode-vs-carry-forward selector: if any in-period row carries
  a printed ``running_balance`` we snap to the last printed close per
  day; otherwise we carry signed amounts forward from the inferred
  beginning balance.
* ``adb_as_pct_of_monthly_deposits`` — ``avg_daily_balance /
  avg_monthly_gross_deposits * 100``. ``None`` when gross deposits are
  zero / negative (the chip then renders "—" rather than a misleading
  0%). Gross deposits == positive amounts in
  ``_GROSS_DEPOSIT_CATEGORIES`` summed over the period, normalised to
  30 days via ``total / period_days * 30`` (same length-invariance
  trick ``_debt_to_revenue`` uses).
* ``negative_days`` — distinct dates whose end-of-day closing balance
  was ``<= 0``. The user spec includes ``= 0`` as a negative day; this
  diverges from ``parser.aggregate._days_negative`` which uses strict
  ``<``. The two surfaces answer different questions: the legacy
  ``days_negative`` is the count of TRUE overdraft days, this surface
  is the count of TIGHT-CASH days (zero or below is a cashflow-stress
  signal even if the bank didn't return items).
* ``negative_days_trailing_3m`` — same count restricted to the last
  90 days of the observation window. When ``period_days <= 90`` the
  two counts are equal by construction.
* ``lowest_balance`` + ``lowest_balance_date`` — minimum closing
  balance reached during the period, with the calendar date it
  bottomed out.

Every monetary figure carries source transaction ids per the AEGIS
audit-trail rule (CLAUDE.md "Auditability — every aggregate stores its
sources"). The count fields also carry source ids — every row on a
negative-balance day — so the dossier drill-down can list specific
PDF page/line citations behind a chip count.

DECISION-BOUNDARY POSTURE — SHADOW ONLY
---------------------------------------
``LOW_ADB_PCT_THRESHOLD = 5`` and
``NEGATIVE_DAYS_TRAILING_3M_THRESHOLD = 8`` gates emit
``low_adb_shadow:{pct}%`` / ``negative_days_shadow:{count}`` entries
on ``BalanceHealthAggregation.shadow_triggers`` — they do NOT
auto-decline. The existing live rules in ``aegis.scoring.score``
(``DAYS_NEGATIVE_HARD_DECLINE = 15`` on full-period days_negative,
plus ADB-derived soft-scoring bands) continue to govern the
decline-boundary. Per CLAUDE.md "Decision-boundary changes —
shadow-first": validate against the corpus before any flip from
shadow to live, and the flip itself is a config change, not code.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money
from aegis.parser.models import ClassifiedTransaction

LOW_ADB_PCT_THRESHOLD: Final[Decimal] = Decimal("5")
"""ADB-as-percent-of-monthly-deposits shadow threshold. Below this
value, emit ``low_adb_shadow``. Strict ``<`` — exactly 5% does not
fire."""

NEGATIVE_DAYS_TRAILING_3M_THRESHOLD: Final[int] = 8
"""Trailing-3m negative-day shadow threshold. Above this count, emit
``negative_days_shadow``. Strict ``>`` — exactly 8 does not fire."""

TRAILING_WINDOW_DAYS: Final[int] = 90
"""Length of the trailing window used for
``negative_days_trailing_3m``. 90 calendar days = "trailing 3 months"
in MCA-shop parlance."""

_MONTH_DAYS: Final[Decimal] = Decimal("30")
"""Calendar-month normalisation. Mirrors ``_debt_to_revenue``'s
``true_revenue * 30 / period_days`` projection so short/long
statements yield comparable monthly-deposit denominators."""

_GROSS_DEPOSIT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"deposit", "ach_credit", "wire_in", "refund"}
)
"""Categories that count as gross inflow for the ADB-vs-deposits
ratio. Mirrors ``parser.aggregate._REVENUE_INCLUDED`` but without the
transfer/chargeback subtraction step — the denominator here is GROSS
deposits, not net revenue (the latter is consumed by
``debt_to_revenue`` elsewhere)."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class BalanceHealthAggregation(_StrictModel):
    """Balance-health rollup for one merchant's observation window.

    Every monetary metric stores the list of contributing
    ``transaction_id``s per the AEGIS audit-trail rule. Count metrics
    carry source ids too — every row on a negative-balance day, every
    row that contributed to the gross-deposit denominator — so the
    dossier drill-down can list specific PDF page/line citations
    behind any chip number.
    """

    avg_daily_balance: Money = Field(
        description=(
            "Time-weighted closing balance averaged over ``period_days``. "
            "Printed-mode: snaps to ``running_balance`` on the last in-day "
            "row when present. Carry-forward fallback: signed-amount roll "
            "from inferred ``beginning_balance``."
        ),
    )
    avg_daily_balance_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description=(
            "Every classified transaction inside the observation window. "
            "ADB is computed from per-day closings; the sources list is "
            "the union of contributing rows."
        ),
    )
    adb_as_pct_of_monthly_deposits: Decimal | None = Field(
        default=None,
        description=(
            "``avg_daily_balance / avg_monthly_gross_deposits * 100``. "
            "``None`` when gross deposits are zero / negative — the chip "
            "suppresses rather than rendering a misleading 0%."
        ),
    )
    adb_as_pct_of_monthly_deposits_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description=(
            "Positive-amount transactions in `_GROSS_DEPOSIT_CATEGORIES` "
            "that contributed to the denominator. Drill-down lists each "
            "row's page/line citation."
        ),
    )
    negative_days: int = Field(
        ge=0,
        description=(
            "Distinct calendar dates whose end-of-day closing balance "
            "was ``<= 0``. Includes the exact-zero case as a tight-cash "
            "signal even though the bank didn't necessarily return items."
        ),
    )
    negative_days_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description="Every transaction posted on a negative-balance day.",
    )
    negative_days_trailing_3m: int = Field(
        ge=0,
        description=(
            "``negative_days`` restricted to the last 90 days of the "
            "observation window. Equal to ``negative_days`` by construction "
            "when ``period_days <= 90``."
        ),
    )
    negative_days_trailing_3m_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description=(
            "Every transaction posted on a negative-balance day inside the trailing 90-day window."
        ),
    )
    lowest_balance: Money = Field(
        description=(
            "Minimum end-of-day closing balance reached during the period. "
            "Can be negative; not floored at zero — the magnitude IS the "
            "signal."
        ),
    )
    lowest_balance_date: date | None = Field(
        default=None,
        description=(
            "Calendar date on which ``lowest_balance`` was reached. "
            "``None`` when the period had no transactions (lowest_balance "
            "defaults to 0 in that case)."
        ),
    )
    lowest_balance_source_ids: tuple[UUID, ...] = Field(
        default_factory=tuple,
        description=(
            "Transactions posted on ``lowest_balance_date``. Drill-down "
            "shows the rows that produced the day's close."
        ),
    )
    shadow_triggers: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Shadow-mode decline annotations. Possible entries: "
            "``low_adb_shadow:{pct}%`` when ADB %% < 5, "
            "``negative_days_shadow:{count}`` when trailing-3m days > 8. "
            "Operator-facing only — not consumed by the live decline path."
        ),
    )


def compute_balance_health(
    transactions: list[ClassifiedTransaction],
    period_days: int,
) -> BalanceHealthAggregation:
    """Compute the balance-health rollup for the dossier chips.

    Parameters
    ----------
    transactions
        Every classified transaction in the observation window. Both
        deposits and withdrawals are consumed: deposits drive the
        gross-deposit denominator (filtered to
        ``_GROSS_DEPOSIT_CATEGORIES``), and all rows participate in
        the closing-balance series.
    period_days
        Observation-window length in days. The function derives
        ``period_end`` from ``max(t.posted_date)`` and computes
        ``period_start = period_end - (period_days - 1) days`` so the
        window has exactly ``period_days`` calendar entries.
        ``period_days < 1`` is clamped to 1 to avoid divide-by-zero on
        a no-data edge case (matches ``mca_stack.aggregate_mca_stack``).

    Returns
    -------
    BalanceHealthAggregation
        Always populated, never ``None``. Empty transactions yields
        an aggregation with zeros and empty source-id tuples — the
        caller decides whether to render the chips.
    """
    if not transactions:
        return BalanceHealthAggregation(
            avg_daily_balance=Decimal("0.00"),
            avg_daily_balance_source_ids=(),
            adb_as_pct_of_monthly_deposits=None,
            adb_as_pct_of_monthly_deposits_source_ids=(),
            negative_days=0,
            negative_days_source_ids=(),
            negative_days_trailing_3m=0,
            negative_days_trailing_3m_source_ids=(),
            lowest_balance=Decimal("0.00"),
            lowest_balance_date=None,
            lowest_balance_source_ids=(),
            shadow_triggers=(),
        )

    safe_period_days = period_days if period_days >= 1 else 1

    period_end = max(t.posted_date for t in transactions)
    period_start = period_end - timedelta(days=safe_period_days - 1)

    by_day: defaultdict[date, list[ClassifiedTransaction]] = defaultdict(list)
    for t in transactions:
        if period_start <= t.posted_date <= period_end:
            by_day[t.posted_date].append(t)

    # Derive ``beginning_balance`` from the first in-period row whose
    # ``running_balance`` is set. Pre-close = running_balance - amount.
    # Without printed balances we fall back to 0 — same posture as
    # ``parser.aggregate._avg_daily_balance``'s carry-forward mode.
    in_period_sorted = sorted(
        (t for ts in by_day.values() for t in ts),
        key=lambda t: (t.posted_date, t.source_page, t.source_line),
    )
    beginning_balance = Decimal("0")
    if in_period_sorted:
        first = in_period_sorted[0]
        if first.running_balance is not None:
            beginning_balance = first.running_balance - first.amount

    # Walk every date in the period and compute the end-of-day close.
    closing_by_date: dict[date, Decimal] = {}
    rows_by_date: dict[date, list[ClassifiedTransaction]] = {}
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
            rows_by_date[cursor] = rows
        closing_by_date[cursor] = closing
        cursor = cursor + timedelta(days=1)

    avg_daily_balance = (
        sum(closing_by_date.values(), Decimal("0")) / Decimal(safe_period_days)
    ).quantize(Decimal("0.01"))

    # Gross deposits ``_GROSS_DEPOSIT_CATEGORIES`` and positive amounts.
    deposit_txs = [
        t for t in in_period_sorted if t.amount > 0 and t.category in _GROSS_DEPOSIT_CATEGORIES
    ]
    deposit_total = sum((t.amount for t in deposit_txs), Decimal("0"))
    avg_monthly_deposits = (deposit_total / Decimal(safe_period_days) * _MONTH_DAYS).quantize(
        Decimal("0.01")
    )

    adb_as_pct: Decimal | None
    if avg_monthly_deposits <= 0:
        adb_as_pct = None
    else:
        adb_as_pct = (avg_daily_balance / avg_monthly_deposits * Decimal("100")).quantize(
            Decimal("0.01")
        )

    # Negative-day counts. Spec: end-of-day closing ``<= 0`` (the exact-
    # zero case is a tight-cash signal too). Source ids = every row on
    # a negative-balance day.
    negative_dates_full = sorted(d for d, c in closing_by_date.items() if c <= 0)
    negative_days = len(negative_dates_full)
    negative_day_sources: list[UUID] = []
    for d in negative_dates_full:
        negative_day_sources.extend(r.id for r in rows_by_date.get(d, []))

    trailing_window_start = max(period_start, period_end - timedelta(days=TRAILING_WINDOW_DAYS - 1))
    negative_dates_3m = [d for d in negative_dates_full if d >= trailing_window_start]
    negative_days_trailing_3m = len(negative_dates_3m)
    negative_day_sources_3m: list[UUID] = []
    for d in negative_dates_3m:
        negative_day_sources_3m.extend(r.id for r in rows_by_date.get(d, []))

    # Lowest balance + date. Tie-break: earliest date wins (older
    # bottom is the original cashflow stress event).
    lowest_date = min(
        closing_by_date.keys(),
        key=lambda d: (closing_by_date[d], d),
    )
    lowest_balance = closing_by_date[lowest_date].quantize(Decimal("0.01"))
    lowest_sources = tuple(r.id for r in rows_by_date.get(lowest_date, []))

    triggers: list[str] = []
    if adb_as_pct is not None and adb_as_pct < LOW_ADB_PCT_THRESHOLD:
        triggers.append(f"low_adb_shadow:{adb_as_pct}%")
    if negative_days_trailing_3m > NEGATIVE_DAYS_TRAILING_3M_THRESHOLD:
        triggers.append(f"negative_days_shadow:{negative_days_trailing_3m}")

    all_in_period_ids = tuple(t.id for t in in_period_sorted)
    deposit_ids = tuple(t.id for t in deposit_txs)

    return BalanceHealthAggregation(
        avg_daily_balance=avg_daily_balance,
        avg_daily_balance_source_ids=all_in_period_ids,
        adb_as_pct_of_monthly_deposits=adb_as_pct,
        adb_as_pct_of_monthly_deposits_source_ids=deposit_ids,
        negative_days=negative_days,
        negative_days_source_ids=tuple(negative_day_sources),
        negative_days_trailing_3m=negative_days_trailing_3m,
        negative_days_trailing_3m_source_ids=tuple(negative_day_sources_3m),
        lowest_balance=lowest_balance,
        lowest_balance_date=lowest_date,
        lowest_balance_source_ids=lowest_sources,
        shadow_triggers=tuple(triggers),
    )


__all__ = [
    "LOW_ADB_PCT_THRESHOLD",
    "NEGATIVE_DAYS_TRAILING_3M_THRESHOLD",
    "TRAILING_WINDOW_DAYS",
    "BalanceHealthAggregation",
    "compute_balance_health",
]
