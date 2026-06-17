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
- `extraction_row_count_mismatch` — extracted row count differs from printed
                                    count by more than max(3, printed * 2%)

Shadow soft-flags (emitted into ValidationResult.warnings; do NOT route to
manual_review on their own — operator validates against corpus, then flips
to a hard-flag-that-routes via config)
------------------------------------------------------------------------
- `daily_balance_continuity_break:{date}_expected_{x}_actual_{y}_diff_{d}` —
  per-day reconciliation off by ≥ $0.01 (stricter than the $1.00 tolerance
  on the routing-level check; catches surgical row swaps that shift cents).
- `daily_balance_continuity_breaks_count:{N}` — summary count when ≥1 break.
- `transaction_id_sequence_gap:{from}_{to}_{missing}` — gap in a populated
  sequential transaction-id / reference / confirmation column. Likely
  evidence of deleted rows.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

from aegis.money import money_eq
from aegis.parser.models import ExtractedStatement, Transaction, ValidationResult

# Allowed statement window. 28-31 day cycles dominate; 14-50 covers
# combined / holiday / biweekly close cycles.
MIN_STATEMENT_DAYS = 14
MAX_STATEMENT_DAYS = 50

# Period-tie-out tolerance: $1.00. Per-day reconciliation also $1.00.
_TOL = Decimal("1.00")

# R1.4 shadow check: stricter cent-level tolerance for the per-day
# continuity validator. The existing `_check_daily_running_balance` uses
# the $1.00 tolerance and routes to `manual_review`; this stricter shadow
# check catches sub-dollar drift (surgical row swaps where the deletion
# and the inserted compensator differ by cents — month math passes, day
# math drifts).
_CONTINUITY_TOL = Decimal("0.01")

# R1.5 shadow check: a transaction-id-like field must be populated on at
# least this fraction of rows AND those values must be majority numeric
# before we'll treat it as a real sequence. Below the floor we treat the
# field as "not a sequence" and emit no flag (silent skip per spec).
_TXN_ID_POPULATION_FLOOR = Decimal("0.80")

# Tokens that mark a number in a transaction description as a transaction
# id / reference / confirmation. Matched case-insensitively against a word
# preceding the number (optionally followed by `#`, `:`, or whitespace).
_TXN_ID_TOKEN_PATTERN = re.compile(
    r"\b(?:conf|confirmation|ref|reference|trace|trn|tran|txn|trans|seq|sequence|chk|check)\b"
    r"[\s#:.\-]*"
    r"(?P<num>\d{4,12})",
    re.IGNORECASE,
)

# Word-boundary "deposit" — avoids matching inside "DEPOSITED",
# "REDEPOSIT" etc. Combined with the exclusion set below, this drops the
# false-positive on rows like "DEPOSIT REVERSAL" / "NSF FEE - LATE
# DEPOSIT" which legitimately carry a negative amount.
_DEPOSIT_WORD = re.compile(r"\bdeposit\b", re.IGNORECASE)
_DEPOSIT_NEG_EXCLUSIONS = frozenset({"reversal", "return", "nsf", "fee", "withdrawal"})

# Shadow-mode TD coercion (CORPUS_FINDINGS 2026-06-17). TD Convenience
# Checking's ACCOUNT SUMMARY prints Electronic Payments and Service
# Charges as SEPARATE line items with no consolidated "Total
# Withdrawals" row. Bedrock extracts Electronic Payments into
# `summary.withdrawal_total`, but the transaction stream contains the
# MSF service-charge row too — so `listed_wd = printed_wd + msf`. Shadow
# check logs what a per-bank coercion WOULD do; routing is unchanged
# until the flip is approved per CLAUDE.md decision-boundary discipline.
_TD_SERVICE_CHARGE_PATTERN = re.compile(
    r"\b(?:service\s+charge|maintenance\s+fee|monthly\s+maintenance)\b",
    re.IGNORECASE,
)


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
    clock: Callable[[], date] | None = None,
) -> ValidationResult:
    """Run the deterministic gate. Returns ValidationResult.

    `truncated` is true if pass 1 hit max_tokens (LLM output cut off).
    `today` is injectable for deterministic testing.
    `clock`, when provided, takes precedence over `today` and is invoked to
    obtain the current date. Useful when callers want to inject a
    timezone-aware clock (e.g. always UTC) instead of relying on the worker's
    server-local timezone — `datetime.now().date()` is local time, so a worker
    running in UTC inspecting a statement that closed in EST can otherwise
    falsely flag `future_dated`.
    """
    ctx = _ValidationContext()
    if clock is not None:
        today = clock()
    elif today is None:
        today = datetime.now().date()

    _check_period(statement, today, ctx)
    _check_period_reconciliation(statement, ctx)
    _check_listed_vs_summary(statement, ctx)
    _check_negative_deposits(statement, ctx)
    _check_source_attribution(statement, ctx)
    _check_daily_running_balance(statement, ctx)
    _check_intraday_running_balance(statement, ctx)
    # R1.4 / R1.5 — shadow mode. These emit warnings only; routing is
    # unchanged. Operator validates against corpus before flipping to a
    # routing flag via config.
    _shadow_check_daily_balance_continuity(statement, ctx)
    _shadow_check_transaction_id_sequence_gaps(statement, ctx)
    _shadow_check_td_withdrawal_coercion(statement, ctx)
    if truncated:
        ctx.failures.append("extraction_truncated_retry_required")

    return ValidationResult(
        passed=len(ctx.failures) == 0,
        failures=ctx.failures,
        warnings=ctx.warnings,
    )


# -- individual checks --------------------------------------------------------


def _check_period(statement: ExtractedStatement, today: date, ctx: _ValidationContext) -> None:
    s, e = statement.summary.period_start, statement.summary.period_end
    if e < s:
        ctx.failures.append("invalid_period: end before start")
        return
    days = (e - s).days
    if days < MIN_STATEMENT_DAYS or days > MAX_STATEMENT_DAYS:
        ctx.failures.append(f"invalid_period: {days} days outside 14-50")
    if e > today:
        ctx.failures.append(f"future_dated: period_end={e} today={today}")


def _check_period_reconciliation(statement: ExtractedStatement, ctx: _ValidationContext) -> None:
    """begin + sum(positive) - abs(sum(negative)) = ending, within $1."""
    summary = statement.summary
    deposits = sum((t.amount for t in statement.transactions if t.amount > 0), Decimal("0"))
    withdrawals_neg = sum((t.amount for t in statement.transactions if t.amount < 0), Decimal("0"))
    expected = summary.beginning_balance + deposits + withdrawals_neg
    if not money_eq(expected, summary.ending_balance, tol=_TOL):
        ctx.failures.append(
            f"reconciliation_failed_period: expected {expected} got {summary.ending_balance}"
        )


def _check_listed_vs_summary(statement: ExtractedStatement, ctx: _ValidationContext) -> None:
    """Sum of extracted deposits/withdrawals must match the printed totals."""
    summary = statement.summary
    listed_dep = sum((t.amount for t in statement.transactions if t.amount > 0), Decimal("0"))
    listed_wd = sum((-t.amount for t in statement.transactions if t.amount < 0), Decimal("0"))
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

    # Hard check: count parity if the bank printed a count.
    #
    # Previously a soft warning at ±3. Real-statement testing showed that
    # ±3 leniency on a 50-row statement (6%) is appropriate, but on an
    # 800-row Chase Business statement, 3 missed rows is hardly anything
    # — we want absolute parity on large statements. Scale by 2% with a
    # floor of 3 so small statements still get the prior leniency.
    if summary.printed_transaction_count is not None:
        diff = abs(len(statement.transactions) - summary.printed_transaction_count)
        threshold = max(3, int(summary.printed_transaction_count * 0.02))
        if diff > threshold:
            ctx.failures.append(
                f"extraction_row_count_mismatch: listed {len(statement.transactions)} "
                f"vs printed {summary.printed_transaction_count} "
                f"(tolerance {threshold})"
            )


def _check_negative_deposits(statement: ExtractedStatement, ctx: _ValidationContext) -> None:
    """Sanity: rows printed as deposits should not be negative.

    Heuristic — emitted as a warning, not a failure. We want to flag the
    case where the LLM swaps signs on a real deposit, but NOT trip on
    rows whose description happens to contain the word "deposit" while
    legitimately carrying a negative amount (e.g. "DEPOSIT REVERSAL",
    "NSF FEE - LATE DEPOSIT", "DEPOSIT RETURN", "WITHDRAWAL OF DEPOSIT").
    """
    for txn in statement.transactions:
        if txn.amount >= 0:
            continue
        desc = txn.description
        if not _DEPOSIT_WORD.search(desc):
            continue
        desc_lower = desc.lower()
        if any(token in desc_lower for token in _DEPOSIT_NEG_EXCLUSIONS):
            continue
        ctx.warnings.append(f"negative_deposit_signal: row '{desc[:40]}' has amount={txn.amount}")


def _check_source_attribution(statement: ExtractedStatement, ctx: _ValidationContext) -> None:
    """Verify every transaction carries page+line attribution.

    Pydantic enforces ge=1 (presence). This routine adds a soft check
    that warns when two transactions share the same source_line on the
    same page (multi-column layouts can legitimately do this; the prior
    rule rejected real PDFs from Chase/PNC/WF when Bedrock returned
    duplicate line numbers for transactions printed side-by-side).

    The warning still surfaces in the parse report so an auditor can see
    "page 6 had duplicate line numbers". The deterministic re-number
    happens in ``aegis.parser.extract`` so all downstream code sees
    monotonic lines per page.
    """
    by_page: defaultdict[int, list[int]] = defaultdict(list)
    for txn in statement.transactions:
        by_page[txn.source_page].append(txn.source_line)
    for page, lines in by_page.items():
        if len(lines) != len(set(lines)):
            ctx.warnings.append(
                f"duplicate_source_line: page {page} had duplicate "
                "source_line values (auto-renumbered preserve audit ordering)"
            )


def _check_daily_running_balance(statement: ExtractedStatement, ctx: _ValidationContext) -> None:
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


def _check_intraday_running_balance(statement: ExtractedStatement, ctx: _ValidationContext) -> None:
    """Verify running_balance is monotonic within a single day.

    The day-end check (_check_daily_running_balance) only sees the last
    row's balance for each day. That leaves a hallucination path: Claude
    could invent intermediate running_balance values and as long as the
    day-end ties to the next day's start, the math passes.

    This check closes that path: for every consecutive pair of rows on
    the same day where BOTH carry a printed running_balance, verify
    `next.running_balance == prev.running_balance + next.amount` within
    the existing $1.00 tolerance. Rows with running_balance=None are
    skipped (best-effort, matching the day-end logic).
    """
    by_day: defaultdict[date, list[Transaction]] = defaultdict(list)
    for txn in statement.transactions:
        by_day[txn.posted_date].append(txn)

    intraday_mismatches: list[str] = []
    for day in sorted(by_day.keys()):
        rows = by_day[day]
        if len(rows) < 2:
            continue
        prev_bal: Decimal | None = None
        for row in rows:
            if row.running_balance is None:
                # Reset chain: we can't verify across a None gap.
                prev_bal = None
                continue
            if prev_bal is not None:
                expected = prev_bal + row.amount
                if not money_eq(expected, row.running_balance, tol=_TOL):
                    intraday_mismatches.append(
                        f"{day.isoformat()} p{row.source_page}l{row.source_line}: "
                        f"expected {expected} got {row.running_balance}"
                    )
            prev_bal = row.running_balance

    if intraday_mismatches:
        sample = intraday_mismatches[:3]
        ctx.failures.append("reconciliation_failed_intraday: " + "; ".join(sample))
        if len(intraday_mismatches) > 3:
            ctx.warnings.append(
                f"reconciliation_failed_intraday_count: {len(intraday_mismatches)} rows"
            )


# -- R1.4 shadow: daily balance continuity ---------------------------------


@dataclass(frozen=True)
class DailyContinuityBreak:
    """One day's continuity break, for shadow-flag emission and tests.

    `expected` is `previous_day_eod_balance + Σ(today's amounts)`.
    `actual` is the printed `running_balance` on the LAST transaction of
    the day. `diff = actual - expected`. Days whose last row carries no
    printed running balance are SKIPPED (not represented in this list)
    because they cannot be validated either way.
    """

    day: date
    expected: Decimal
    actual: Decimal
    diff: Decimal


def validate_daily_balance_continuity(
    transactions: list[Transaction],
    beginning_balance: Decimal,
    period_start: date,
    period_end: date,
) -> list[DailyContinuityBreak]:
    """Per-day reconciliation walk with cent-level tolerance.

    Sorts transactions deterministically (posted_date, then source_page,
    then source_line) and walks day by day. For each day with at least one
    transaction AND a printed running_balance on the LAST transaction of
    that day, compute:

        expected_eod = previous_day_eod + Σ(today's amounts)

    where `previous_day_eod` is the last printed running_balance from a
    prior day (or `beginning_balance` if no prior day printed one). Any
    abs(diff) >= $0.01 is reported as a `DailyContinuityBreak`.

    Days without a printed end-of-day running_balance are SKIPPED and do
    NOT advance `previous_day_eod` — we never derive a balance from raw
    amount sums, only from printed totals, so we can't claim a day "ties"
    when the bank didn't print its number.

    Transactions outside `[period_start, period_end]` are ignored; the
    caller's existing period-window checks handle out-of-window rows.
    """
    if not transactions:
        return []

    in_window = [txn for txn in transactions if period_start <= txn.posted_date <= period_end]
    if not in_window:
        return []

    by_day: defaultdict[date, list[Transaction]] = defaultdict(list)
    for txn in in_window:
        by_day[txn.posted_date].append(txn)

    # Stable, deterministic intra-day ordering: source_page then source_line.
    for day in by_day:
        by_day[day].sort(key=lambda t: (t.source_page, t.source_line))

    days_sorted = sorted(by_day.keys())
    breaks: list[DailyContinuityBreak] = []
    prev_eod: Decimal = beginning_balance

    for day in days_sorted:
        rows = by_day[day]
        last_row = rows[-1]
        if last_row.running_balance is None:
            # Bank did not print an EOD running balance for this day. The
            # spec requires SKIP — we cannot validate this day in either
            # direction, and we must not advance `prev_eod` from raw sums.
            continue

        day_delta = sum((r.amount for r in rows), Decimal("0"))
        expected_eod = prev_eod + day_delta
        actual_eod = last_row.running_balance
        diff = actual_eod - expected_eod

        if abs(diff) >= _CONTINUITY_TOL:
            breaks.append(
                DailyContinuityBreak(
                    day=day,
                    expected=expected_eod,
                    actual=actual_eod,
                    diff=diff,
                )
            )

        # Always anchor to the printed balance, never to expected — the
        # check's purpose is to detect drift between printed values, so
        # carrying expected forward would mask compounding breaks.
        prev_eod = actual_eod

    return breaks


def _shadow_check_daily_balance_continuity(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """Emit per-break shadow flags + a summary count. No routing change."""
    breaks = validate_daily_balance_continuity(
        statement.transactions,
        beginning_balance=statement.summary.beginning_balance,
        period_start=statement.summary.period_start,
        period_end=statement.summary.period_end,
    )
    if not breaks:
        return
    for brk in breaks:
        ctx.warnings.append(
            f"daily_balance_continuity_break:{brk.day.isoformat()}"
            f"_expected_{brk.expected}_actual_{brk.actual}_diff_{brk.diff}"
        )
    ctx.warnings.append(f"daily_balance_continuity_breaks_count:{len(breaks)}")


# -- R1.5 shadow: transaction-id sequence gaps -----------------------------


@dataclass(frozen=True)
class GapEvidence:
    """One gap in a populated sequential transaction-id column."""

    from_id: int
    to_id: int
    count_missing: int
    suspected_position: int  # 0-indexed; index of `from_id` in the sorted ids list


def _extract_txn_id(description: str) -> int | None:
    """Pull a numeric transaction-id-looking value from a description.

    Looks for tokens like `CONF#`, `REF:`, `TRACE`, `TRN`, `SEQ`, `CHK`
    (case-insensitive) immediately followed by a 4-12 digit number.
    Returns the first match as an int, or None. We deliberately require
    the keyword prefix so we don't pick up dollar amounts, ZIPs, dates,
    etc. that happen to live in the description.
    """
    match = _TXN_ID_TOKEN_PATTERN.search(description)
    if match is None:
        return None
    try:
        return int(match.group("num"))
    except ValueError:
        return None


def detect_transaction_id_sequence_gaps(
    transactions: list[Transaction],
) -> list[GapEvidence]:
    """Find gaps in a sequential transaction-id field, if one exists.

    Algorithm (per spec):
      1. Scan descriptions for a transaction-id-shaped token (see
         `_TXN_ID_TOKEN_PATTERN`).
      2. If ≥ 80% of rows yield a numeric id, treat it as a real sequence.
         Below the floor → return [] (silent skip).
      3. Sort the ids ascending, walk consecutive pairs, emit a
         `GapEvidence` per pair whose delta > 1.

    The 80% population floor is intentional — many statements (Chase,
    Wells) print confirmation numbers on some rows but not on transfers,
    fees, or check images. If only 30% of rows have ids, the "missing"
    ones might just be unprinted, not deleted.
    """
    if not transactions:
        return []

    extracted: list[int] = []
    for txn in transactions:
        txn_id = _extract_txn_id(txn.description)
        if txn_id is not None:
            extracted.append(txn_id)

    if not extracted:
        return []

    population_rate = Decimal(len(extracted)) / Decimal(len(transactions))
    if population_rate < _TXN_ID_POPULATION_FLOOR:
        return []

    # De-dup (some banks re-print the same conf number on a paired credit
    # / debit) and sort ascending.
    unique_ids = sorted(set(extracted))
    if len(unique_ids) < 2:
        return []

    gaps: list[GapEvidence] = []
    for position, (from_id, to_id) in enumerate(pairwise(unique_ids)):
        delta = to_id - from_id
        if delta > 1:
            gaps.append(
                GapEvidence(
                    from_id=from_id,
                    to_id=to_id,
                    count_missing=delta - 1,
                    suspected_position=position,
                )
            )
    return gaps


def _shadow_check_transaction_id_sequence_gaps(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """Emit a shadow flag per detected gap. No routing change."""
    gaps = detect_transaction_id_sequence_gaps(statement.transactions)
    for gap in gaps:
        ctx.warnings.append(
            f"transaction_id_sequence_gap:{gap.from_id}_{gap.to_id}_{gap.count_missing}"
        )


# -- TD withdrawal-total coercion (shadow) ---------------------------------


def _is_td_bank(bank_name: str | None) -> bool:
    """True when the statement's bank_name looks like TD Bank.

    Case-insensitive substring on ``td bank`` covers the variants the
    Bedrock extractor returns across TD Convenience Checking, TD
    Business Convenience, and TD Bank N.A. headers. Restrictive enough
    that ``Wells Fargo Plus`` or ``BTD Brokerage`` won't false-match —
    neither contains ``td bank`` as a substring.
    """
    if not bank_name:
        return False
    return "td bank" in bank_name.lower()


def td_service_charge_total(transactions: list[Transaction]) -> Decimal:
    """Sum of withdrawals (positive magnitude) whose description matches
    the TD service-charge / maintenance-fee pattern. Withdrawals only —
    a positive-amount row that happens to mention "maintenance fee"
    (e.g. an inbound refund of a prior MSF) is not what we're accounting
    for here.
    """
    return sum(
        (
            -t.amount
            for t in transactions
            if t.amount < 0 and _TD_SERVICE_CHARGE_PATTERN.search(t.description)
        ),
        Decimal("0"),
    )


def _shadow_check_td_withdrawal_coercion(
    statement: ExtractedStatement, ctx: _ValidationContext
) -> None:
    """Log what a TD-specific withdrawal-total coercion WOULD do.

    Routing unchanged. When the bank looks like TD AND
    ``listed_wd > printed_wd`` by more than the standard $1 tolerance,
    compute the residual after subtracting matched service-charge rows.
    Two distinct warnings so a corpus / live-shadow window can
    distinguish the explainable cases from drift the rule wouldn't
    cover:

      * ``shadow_td_withdrawal_coercion_would_clear:...`` — drift ≈
        service-charge subtotal within $1. The proposed per-bank
        coercion (``printed_wd += service_charges``) would pass this
        document under the existing $1 reconciliation tolerance.
      * ``shadow_td_withdrawal_drift_unattributed:...`` — drift NOT
        explained by service-charge rows. Coercion would NOT clear this
        document; another mechanism is in play.

    Only fires on the ``listed > printed`` direction (the TD
    summary-split direction documented in CORPUS_FINDINGS 2026-06-17).
    The reverse direction (printed > listed) would imply Bedrock
    dropped withdrawal rows, a different bug class and not what
    coercion would address.
    """
    summary = statement.summary
    if not _is_td_bank(summary.bank_name):
        return

    listed_wd = sum(
        (-t.amount for t in statement.transactions if t.amount < 0),
        Decimal("0"),
    )
    drift = listed_wd - summary.withdrawal_total
    if drift <= _TOL:
        return

    service_charges = td_service_charge_total(statement.transactions)
    residual = drift - service_charges
    if abs(residual) <= _TOL:
        ctx.warnings.append(
            f"shadow_td_withdrawal_coercion_would_clear:"
            f"listed_{listed_wd}_printed_{summary.withdrawal_total}"
            f"_service_charges_{service_charges}_residual_{residual}"
        )
    else:
        ctx.warnings.append(
            f"shadow_td_withdrawal_drift_unattributed:"
            f"listed_{listed_wd}_printed_{summary.withdrawal_total}"
            f"_service_charges_{service_charges}_residual_{residual}"
        )


__all__ = [
    "MAX_STATEMENT_DAYS",
    "MIN_STATEMENT_DAYS",
    "DailyContinuityBreak",
    "GapEvidence",
    "detect_transaction_id_sequence_gaps",
    "td_service_charge_total",
    "validate_daily_balance_continuity",
    "validate_extraction",
]
