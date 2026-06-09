"""Tests for the R1.4 / R1.5 shadow-mode continuity validators.

These two validators emit soft warnings only; they do NOT mutate
`parse_status` routing. The hard daily / intraday running-balance checks
in `validate_extraction` still own decline routing. Once the operator
validates these shadow flags against the corpus, the flip to a routing
flag is a config change, not a code change.

Why a stricter ($0.01) tolerance is the value-add here
------------------------------------------------------
`_check_daily_running_balance` already runs in `validate_extraction` with
a $1.00 tolerance and routes mismatches to `failures`. The shadow check
adds a per-break warning at the cent level — surgical splices (e.g.
deleting one row, inserting a fake row whose cents differ) drift under
the dollar tolerance but break cleanly at the cent tolerance.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aegis.parser.models import (
    ExtractedStatement,
    StatementSummary,
    Transaction,
)
from aegis.parser.validate import (
    DailyContinuityBreak,
    GapEvidence,
    detect_transaction_id_sequence_gaps,
    validate_daily_balance_continuity,
    validate_extraction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_stmt(
    transactions: list[Transaction],
    *,
    beginning: Decimal = Decimal("1000.00"),
    ending: Decimal | None = None,
    deposits: Decimal | None = None,
    withdrawals: Decimal | None = None,
    period_start: date = date(2026, 1, 1),
    period_end: date = date(2026, 1, 20),
) -> ExtractedStatement:
    """Build a statement that ties out at the period level by default.

    Tests that want to make the daily-continuity check fire WITHOUT
    breaking the period reconciliation pass `ending`/`deposits`/
    `withdrawals` directly. Otherwise we derive them from the transaction
    list so the rest of the validation gate stays clean.
    """
    if deposits is None:
        deposits = sum(
            (t.amount for t in transactions if t.amount > 0), Decimal("0")
        )
    if withdrawals is None:
        withdrawals = sum(
            (-t.amount for t in transactions if t.amount < 0), Decimal("0")
        )
    if ending is None:
        ending = beginning + deposits - withdrawals
    return ExtractedStatement(
        summary=StatementSummary(
            beginning_balance=beginning,
            ending_balance=ending,
            deposit_total=deposits,
            withdrawal_total=withdrawals,
            period_start=period_start,
            period_end=period_end,
        ),
        transactions=transactions,
    )


def _txn(
    day: int,
    amount: str,
    *,
    running_balance: str | None,
    description: str = "TRANSACTION",
    source_page: int = 1,
    source_line: int = 1,
) -> Transaction:
    rb = Decimal(running_balance) if running_balance is not None else None
    return Transaction(
        posted_date=date(2026, 1, day),
        description=description,
        amount=Decimal(amount),
        running_balance=rb,
        source_page=source_page,
        source_line=source_line,
    )


# ---------------------------------------------------------------------------
# R1.4 — validate_daily_balance_continuity
# ---------------------------------------------------------------------------


def test_continuity_happy_path_no_breaks() -> None:
    """Consistent running balance on every day → zero breaks."""
    txns = [
        # Day 5: +500 then -200 → eod 1300
        _txn(5, "500.00", running_balance="1500.00", source_line=1),
        _txn(5, "-200.00", running_balance="1300.00", source_line=2),
        # Day 10: +200 → eod 1500
        _txn(10, "200.00", running_balance="1500.00", source_line=1),
        # Day 15: -300 → eod 1200
        _txn(15, "-300.00", running_balance="1200.00", source_line=1),
    ]
    stmt = _build_stmt(txns)
    breaks = validate_daily_balance_continuity(
        stmt.transactions,
        beginning_balance=stmt.summary.beginning_balance,
        period_start=stmt.summary.period_start,
        period_end=stmt.summary.period_end,
    )
    assert breaks == []

    # Shadow flag also absent from the gate output.
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not any(
        w.startswith("daily_balance_continuity_break") for w in result.warnings
    )
    assert not any(
        w.startswith("daily_balance_continuity_breaks_count")
        for w in result.warnings
    )


def test_continuity_single_break_day_off_by_one_thousand() -> None:
    """Day 5 ends $1000 below where it should be.

    Begin 1000. Day 5 deposit +1000 → expected eod 2000, but the printed
    running balance reads 1000 (broken). Days 10 / 15 anchor off the
    printed (broken) 1000 so they tie internally — only day 5 breaks.
    """
    txns = [
        # Day 5: +1000, but the bank printed running balance 1000 (should be 2000)
        _txn(5, "1000.00", running_balance="1000.00", source_line=1),
        # Day 10: +500 off the broken 1000 anchor → printed 1500, consistent
        _txn(10, "500.00", running_balance="1500.00", source_line=1),
        # Day 15: -300 → printed 1200, consistent
        _txn(15, "-300.00", running_balance="1200.00", source_line=1),
    ]
    # Keep the period totals matching the actual transactions so we
    # isolate the daily-continuity signal cleanly.
    stmt = _build_stmt(
        txns,
        beginning=Decimal("1000.00"),
        ending=Decimal("1200.00"),
        deposits=Decimal("1500.00"),
        withdrawals=Decimal("300.00"),
    )

    breaks = validate_daily_balance_continuity(
        stmt.transactions,
        beginning_balance=stmt.summary.beginning_balance,
        period_start=stmt.summary.period_start,
        period_end=stmt.summary.period_end,
    )
    assert len(breaks) == 1
    break_ = breaks[0]
    assert break_.day == date(2026, 1, 5)
    assert break_.expected == Decimal("2000.00")
    assert break_.actual == Decimal("1000.00")
    assert break_.diff == Decimal("-1000.00")

    result = validate_extraction(stmt, today=date(2026, 1, 25))
    matching = [
        w for w in result.warnings if w.startswith("daily_balance_continuity_break:")
    ]
    assert len(matching) == 1, matching
    flag = matching[0]
    assert "2026-01-05" in flag
    assert "expected_2000.00" in flag
    assert "actual_1000.00" in flag
    assert "diff_-1000.00" in flag

    counts = [
        w
        for w in result.warnings
        if w.startswith("daily_balance_continuity_breaks_count:")
    ]
    assert counts == ["daily_balance_continuity_breaks_count:1"]


def test_continuity_multiple_breaks_emits_per_break_and_count() -> None:
    """Splicing scenario: three broken days → ≥2 breaks + the count flag.

    Each day's printed running balance is independently wrong relative to
    the (already-broken) prior anchor, so the walk reports three breaks.
    """
    txns = [
        # Day 3: +500 off 1000 → expected 1500, printed 1400 (off by -100)
        _txn(3, "500.00", running_balance="1400.00", source_line=1),
        # Day 7: +300 off 1400 anchor → expected 1700, printed 1600 (off by -100)
        _txn(7, "300.00", running_balance="1600.00", source_line=1),
        # Day 12: -200 off 1600 anchor → expected 1400, printed 1300 (off by -100)
        _txn(12, "-200.00", running_balance="1300.00", source_line=1),
    ]
    stmt = _build_stmt(
        txns,
        beginning=Decimal("1000.00"),
        ending=Decimal("1300.00"),
        deposits=Decimal("800.00"),
        withdrawals=Decimal("200.00"),
    )

    breaks = validate_daily_balance_continuity(
        stmt.transactions,
        beginning_balance=stmt.summary.beginning_balance,
        period_start=stmt.summary.period_start,
        period_end=stmt.summary.period_end,
    )
    assert len(breaks) == 3
    days_with_breaks = {b.day for b in breaks}
    assert days_with_breaks == {date(2026, 1, 3), date(2026, 1, 7), date(2026, 1, 12)}
    for brk in breaks:
        assert brk.diff == Decimal("-100.00")

    result = validate_extraction(stmt, today=date(2026, 1, 25))
    break_flags = [
        w for w in result.warnings if w.startswith("daily_balance_continuity_break:")
    ]
    assert len(break_flags) == 3

    count_flags = [
        w
        for w in result.warnings
        if w.startswith("daily_balance_continuity_breaks_count:")
    ]
    assert count_flags == ["daily_balance_continuity_breaks_count:3"]


def test_continuity_skips_day_with_no_printed_running_balance() -> None:
    """Last row of day has running_balance=None → that day is SKIPPED.

    The next anchored day computes off the last printed value from a
    PRIOR day, not from a synthesized sum. We assert no break fires for
    the skipped day and that the next day with a printed balance still
    validates correctly.
    """
    txns = [
        # Day 5: anchored at 1500
        _txn(5, "500.00", running_balance="1500.00", source_line=1),
        # Day 8: no running_balance printed on the only row → SKIP
        _txn(8, "200.00", running_balance=None, source_line=1),
        # Day 12: -100 off the LAST PRINTED anchor (1500) → expected 1400
        # (we are NOT synthesizing 1700 from day 8's amount).
        _txn(12, "-100.00", running_balance="1400.00", source_line=1),
    ]
    stmt = _build_stmt(
        txns,
        beginning=Decimal("1000.00"),
        ending=Decimal("1600.00"),
        deposits=Decimal("700.00"),
        withdrawals=Decimal("100.00"),
    )

    breaks = validate_daily_balance_continuity(
        stmt.transactions,
        beginning_balance=stmt.summary.beginning_balance,
        period_start=stmt.summary.period_start,
        period_end=stmt.summary.period_end,
    )
    assert breaks == []

    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not any(
        w.startswith("daily_balance_continuity_break") for w in result.warnings
    )


def test_continuity_tolerance_cent_boundary() -> None:
    """Cent-level boundary behavior.

    The `Money` type is constrained to 2dp at the Pydantic layer, so a
    literal "$0.001" drift cannot be constructed from valid input. The
    spec's intent — sub-cent drift is below tolerance — is enforced
    structurally by that 2dp clamp. What we CAN and MUST test here is
    that the cent-level boundary (`abs(diff) >= $0.01`) is right:

      - 0-cent drift (exact tie) → NOT flagged
      - 2-cent drift → flagged with diff exactly $0.02
    """
    # Exact tie: NOT flagged.
    no_drift_txns = [
        _txn(5, "100.00", running_balance="1100.00", source_line=1),
    ]
    no_drift_breaks = validate_daily_balance_continuity(
        no_drift_txns,
        beginning_balance=Decimal("1000.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 20),
    )
    assert no_drift_breaks == []

    # Two-cent drift: flagged. Demonstrates that the cent-level shadow
    # check catches drift that the dollar-tolerance hard check would miss.
    two_cent_txns = [
        _txn(5, "100.00", running_balance="1100.02", source_line=1),
    ]
    two_cent_breaks = validate_daily_balance_continuity(
        two_cent_txns,
        beginning_balance=Decimal("1000.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 20),
    )
    assert len(two_cent_breaks) == 1
    assert two_cent_breaks[0].diff == Decimal("0.02")


def test_continuity_uses_beginning_balance_as_first_anchor() -> None:
    """First day's expected uses `beginning_balance` as prev_eod."""
    txns = [
        _txn(2, "250.00", running_balance="1250.00", source_line=1),
    ]
    breaks = validate_daily_balance_continuity(
        txns,
        beginning_balance=Decimal("1000.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 20),
    )
    assert breaks == []

    # Same setup but first day's printed balance is wrong:
    bad_txns = [
        _txn(2, "250.00", running_balance="9999.00", source_line=1),
    ]
    bad_breaks = validate_daily_balance_continuity(
        bad_txns,
        beginning_balance=Decimal("1000.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 20),
    )
    assert len(bad_breaks) == 1
    assert bad_breaks[0] == DailyContinuityBreak(
        day=date(2026, 1, 2),
        expected=Decimal("1250.00"),
        actual=Decimal("9999.00"),
        diff=Decimal("8749.00"),
    )


# ---------------------------------------------------------------------------
# R1.5 — detect_transaction_id_sequence_gaps
# ---------------------------------------------------------------------------


def test_id_gap_happy_path_no_gaps() -> None:
    """Consecutive ids (1001, 1002, 1003) → no gap flag."""
    txns = [
        _txn(
            3,
            "100.00",
            running_balance="1100.00",
            description="ACH CREDIT CONF#1001",
            source_line=1,
        ),
        _txn(
            5,
            "150.00",
            running_balance="1250.00",
            description="DEPOSIT REF#1002",
            source_line=1,
        ),
        _txn(
            8,
            "-50.00",
            running_balance="1200.00",
            description="FEE TRACE 1003",
            source_line=1,
        ),
    ]
    gaps = detect_transaction_id_sequence_gaps(txns)
    assert gaps == []


def test_id_gap_single_gap_detected() -> None:
    """Ids (1001, 1002, 1005, 1006) → one gap flag 1002→1005, missing 2."""
    txns = [
        _txn(
            3,
            "100.00",
            running_balance="1100.00",
            description="ACH CONF#1001",
            source_line=1,
        ),
        _txn(
            4,
            "100.00",
            running_balance="1200.00",
            description="ACH CONF#1002",
            source_line=1,
        ),
        _txn(
            7,
            "100.00",
            running_balance="1300.00",
            description="ACH CONF#1005",
            source_line=1,
        ),
        _txn(
            8,
            "100.00",
            running_balance="1400.00",
            description="ACH CONF#1006",
            source_line=1,
        ),
    ]
    gaps = detect_transaction_id_sequence_gaps(txns)
    assert len(gaps) == 1
    assert gaps[0] == GapEvidence(
        from_id=1002,
        to_id=1005,
        count_missing=2,
        suspected_position=1,
    )

    stmt = _build_stmt(
        txns,
        deposits=Decimal("400.00"),
        withdrawals=Decimal("0.00"),
        ending=Decimal("1400.00"),
    )
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    matching = [
        w for w in result.warnings if w.startswith("transaction_id_sequence_gap:")
    ]
    assert matching == ["transaction_id_sequence_gap:1002_1005_2"]


def test_id_gap_no_ids_in_descriptions_no_flag() -> None:
    """Descriptions never carry a transaction-id-shaped token → no flag.

    Population rate is 0%, which is below the 80% floor, so we silently
    skip (per spec).
    """
    txns = [
        _txn(
            3,
            "100.00",
            running_balance="1100.00",
            description="DEPOSIT",
            source_line=1,
        ),
        _txn(
            5,
            "200.00",
            running_balance="1300.00",
            description="ACH CREDIT FROM ACME LLC",
            source_line=1,
        ),
        _txn(
            8,
            "-50.00",
            running_balance="1250.00",
            description="MONTHLY FEE",
            source_line=1,
        ),
    ]
    gaps = detect_transaction_id_sequence_gaps(txns)
    assert gaps == []


def test_id_gap_below_population_floor_no_flag() -> None:
    """Only 25% of rows carry an id → below 80% floor → silent skip."""
    txns = [
        _txn(
            3,
            "100.00",
            running_balance="1100.00",
            description="DEPOSIT CONF#1001",
            source_line=1,
        ),
        _txn(
            5,
            "100.00",
            running_balance="1200.00",
            description="DEPOSIT",
            source_line=1,
        ),
        _txn(
            6,
            "100.00",
            running_balance="1300.00",
            description="ACH CREDIT",
            source_line=1,
        ),
        _txn(
            8,
            "100.00",
            running_balance="1400.00",
            description="WIRE IN FROM ACME",
            source_line=1,
        ),
    ]
    gaps = detect_transaction_id_sequence_gaps(txns)
    assert gaps == []


def test_id_gap_multiple_gaps() -> None:
    """Two gaps in one sequence — each is its own flag."""
    txns = [
        _txn(
            3,
            "100.00",
            running_balance="1100.00",
            description="ACH REF#5000",
            source_line=1,
        ),
        _txn(
            4,
            "100.00",
            running_balance="1200.00",
            description="ACH REF#5001",
            source_line=1,
        ),
        _txn(
            5,
            "100.00",
            running_balance="1300.00",
            description="ACH REF#5005",
            source_line=1,
        ),
        _txn(
            6,
            "100.00",
            running_balance="1400.00",
            description="ACH REF#5006",
            source_line=1,
        ),
        _txn(
            7,
            "100.00",
            running_balance="1500.00",
            description="ACH REF#5010",
            source_line=1,
        ),
    ]
    gaps = detect_transaction_id_sequence_gaps(txns)
    assert len(gaps) == 2
    assert gaps[0].from_id == 5001
    assert gaps[0].to_id == 5005
    assert gaps[0].count_missing == 3
    assert gaps[1].from_id == 5006
    assert gaps[1].to_id == 5010
    assert gaps[1].count_missing == 3
