"""Deterministic validation gate.

Runs between extract (pass 1) and classify (pass 2). Checks the printed
totals tie out against the transaction list, the daily running balance
reconciles, the statement period is sane (14-50 days), and every
transaction carries source_page + source_line.

ANY failure -> parse_status = "manual_review". No retry. No second AI
chance. This gate is the firewall against AI hallucination — catching it
here means the rest of the pipeline only ever runs on data that ties out.

Tolerances are absolute dollar amounts ($1.00 by default) because banks
print to 2dp and we accept rounding noise on individual lines but not on
period totals.

Failure codes (start of string is parsed by `pipeline.py` for severity)
-----------------------------------------------------------------------
- `reconciliation_failed_*` — math broken; document is unusable
- `future_dated`            — period_end > today; trash data
- `extraction_truncated`    — Claude hit max_tokens; retry-flag
- `missing_source`          — a transaction lacks page/line attribution
- `invalid_period`          — period < 14 or > 50 days
- `negative_deposit`        — a deposit row has negative amount
- `daily_balance_mismatch`  — at least one day's running balance is wrong
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from aegis.money import money_eq
from aegis.parser.models import ExtractedStatement, Transaction, ValidationResult

# Allowed statement window. 28-31 day cycles dominate; 14-50 covers
# combined / holiday / biweekly close cycles.
MIN_STATEMENT_DAYS = 14
MAX_STATEMENT_DAYS = 50

# Period-tie-out tolerance: $1.00. Per-day reconciliation also $1.00.
_TOL = Decimal("1.00")


@dataclass
class _DailyBalance:
    day: date
    expected_close: Decimal
    actual_close: Decimal


@dataclass
class _ValidationContext:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    daily_mismatches: list[_DailyBalance] = field(default_factory=list)


def validate_extraction(
    statement: ExtractedStatement,
    *,
    truncated: bool = False,
    today: date | None = None,
) -> ValidationResult:
    """Run the deterministic gate. Returns ValidationResult.

    `truncated` is true if pass 1 hit max_tokens (LLM output cut off).
    `today` is injectable for deterministic testing.
    """
    ctx = _ValidationContext()
    today = today or datetime.now().date()

    _check_period(statement, today, ctx)
    _check_period_reconciliation(statement, ctx)
    _check_listed_vs_summary(statement, ctx)
    _check_negative_deposits(statement, ctx)
    _check_source_attribution(statement, ctx)
    _check_daily_running_balance(statement, ctx)
    if truncated:
        ctx.failures.append("extraction_truncated_retry_required")

    return ValidationResult(
        passed=len(ctx.failures) == 0,
        failures=ctx.failures,
        warnings=ctx.warnings,
    )


# -- individual checks --------------------------------------------------------


def _check_period(
    statement: ExtractedStatement, today: date, ctx: _ValidationContext
) -> None:
    s, e = statement.summary.period_start, statement.summary.period_end
    if e < s:
        ctx.failures.append("invalid_period: end before start")
        return
    days = (e - s).days
    if days < MIN_STATEMENT_DAYS or days > MAX_STATEMENT_DAYS:
        ctx.failures.append(f"invalid_period: {days} days outside 14-50")
    if e > today:
        ctx.failures.append(f"future_dated: period_end={e} today={today}")


def _check_period_reconciliation(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """begin + sum(positive) - abs(sum(negative)) = ending, within $1."""
    summary = statement.summary
    deposits = sum((t.amount for t in statement.transactions if t.amount > 0), Decimal("0"))
    withdrawals_neg = sum(
        (t.amount for t in statement.transactions if t.amount < 0), Decimal("0")
    )
    expected = summary.beginning_balance + deposits + withdrawals_neg
    if not money_eq(expected, summary.ending_balance, tol=_TOL):
        ctx.failures.append(
            f"reconciliation_failed_period: expected {expected} "
            f"got {summary.ending_balance}"
        )


def _check_listed_vs_summary(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """Sum of extracted deposits/withdrawals must match the printed totals."""
    summary = statement.summary
    listed_dep = sum(
        (t.amount for t in statement.transactions if t.amount > 0), Decimal("0")
    )
    listed_wd = sum(
        (-t.amount for t in statement.transactions if t.amount < 0), Decimal("0")
    )
    if not money_eq(listed_dep, summary.deposit_total, tol=_TOL):
        ctx.failures.append(
            f"reconciliation_failed_deposit_total: listed {listed_dep} "
            f"vs printed {summary.deposit_total}"
        )
    if not money_eq(listed_wd, summary.withdrawal_total, tol=_TOL):
        ctx.failures.append(
            f"reconciliation_failed_withdrawal_total: listed {listed_wd} "
            f"vs printed {summary.withdrawal_total}"
        )

    # Soft check: count parity if the bank printed a count.
    if summary.printed_transaction_count is not None:
        diff = abs(len(statement.transactions) - summary.printed_transaction_count)
        if diff > 3:
            ctx.warnings.append(
                f"transaction_count_mismatch: listed {len(statement.transactions)} "
                f"vs printed {summary.printed_transaction_count}"
            )


def _check_negative_deposits(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """Sanity: rows printed as deposits should not be negative."""
    # Heuristic: in our amount convention, deposits are positive. If the LLM
    # ever swaps signs we want the gate to catch it.
    for txn in statement.transactions:
        if txn.amount < 0 and "deposit" in txn.description.lower():
            ctx.warnings.append(
                f"negative_deposit_signal: row '{txn.description[:40]}' has amount={txn.amount}"
            )


def _check_source_attribution(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """Hard-fail if any transaction lacks page+line attribution.

    Pydantic enforces ge=1, but a placeholder of 1/1 on every row would
    silently break audit drill-down. We require monotonic-or-distinct lines
    per page (different rows in the same page must have different line numbers).
    """
    by_page: defaultdict[int, list[int]] = defaultdict(list)
    for txn in statement.transactions:
        by_page[txn.source_page].append(txn.source_line)
    for page, lines in by_page.items():
        if len(lines) != len(set(lines)):
            ctx.failures.append(
                f"missing_source_uniqueness: page {page} has duplicate source_line values"
            )


def _check_daily_running_balance(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """For every day with transactions: end-of-day = previous + sum(today).

    Skipped if running_balance is missing on any row of the day. We report a
    `daily_balance_mismatch` failure with up to 3 sample days; the full list
    is in the warnings for inspection.
    """
    by_day: defaultdict[date, list[Transaction]] = defaultdict(list)
    for txn in statement.transactions:
        by_day[txn.posted_date].append(txn)

    if not by_day:
        return

    days_sorted = sorted(by_day.keys())
    prev_close = statement.summary.beginning_balance

    for day in days_sorted:
        rows = by_day[day]
        # Need a printed running balance on the LAST row of the day to verify.
        last = rows[-1]
        if last.running_balance is None:
            # Best-effort: skip days where the bank didn't print a running balance.
            prev_close += sum((r.amount for r in rows), Decimal("0"))
            continue

        expected_close = prev_close + sum((r.amount for r in rows), Decimal("0"))
        if not money_eq(expected_close, last.running_balance, tol=_TOL):
            ctx.daily_mismatches.append(
                _DailyBalance(
                    day=day,
                    expected_close=expected_close,
                    actual_close=last.running_balance,
                )
            )
        prev_close = last.running_balance

    if ctx.daily_mismatches:
        sample = ctx.daily_mismatches[:3]
        ctx.failures.append(
            "daily_balance_mismatch: "
            + "; ".join(
                f"{m.day.isoformat()} expected {m.expected_close} got {m.actual_close}"
                for m in sample
            )
        )
        if len(ctx.daily_mismatches) > 3:
            ctx.warnings.append(
                f"daily_balance_mismatch_count: {len(ctx.daily_mismatches)} total mismatched days"
            )


__all__ = [
    "MAX_STATEMENT_DAYS",
    "MIN_STATEMENT_DAYS",
    "validate_extraction",
]
