"""Tests for the intra-day running-balance check + hard row-count parity.

These two gates close hallucination paths that the day-end-only balance
check and the prior soft ±3 transaction-count tolerance left open.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aegis.parser.models import (
    ExtractedStatement,
    StatementSummary,
    Transaction,
)
from aegis.parser.validate import validate_extraction


def _stmt(
    transactions: list[Transaction],
    *,
    beginning: Decimal = Decimal("1000.00"),
    ending: Decimal | None = None,
    deposits: Decimal | None = None,
    withdrawals: Decimal | None = None,
    printed_count: int | None = None,
    period_start: date = date(2026, 1, 1),
    period_end: date = date(2026, 1, 20),
) -> ExtractedStatement:
    """Build a clean statement that ties out, then mutate as needed."""
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
            printed_transaction_count=printed_count,
        ),
        transactions=transactions,
    )


def test_intraday_balance_passes_when_consecutive_rows_tie() -> None:
    """Three rows on the same day, each running_balance chain is exact."""
    stmt = _stmt(
        [
            Transaction(
                posted_date=date(2026, 1, 5),
                description="DEPOSIT 1",
                amount=Decimal("300.00"),
                running_balance=Decimal("1300.00"),
                source_page=1,
                source_line=1,
            ),
            Transaction(
                posted_date=date(2026, 1, 5),
                description="DEPOSIT 2",
                amount=Decimal("200.00"),
                running_balance=Decimal("1500.00"),
                source_page=1,
                source_line=2,
            ),
            Transaction(
                posted_date=date(2026, 1, 5),
                description="WITHDRAW",
                amount=Decimal("-200.00"),
                running_balance=Decimal("1300.00"),
                source_page=1,
                source_line=3,
            ),
        ],
    )
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert result.passed, f"chain ties; failures={result.failures!r}"


def test_intraday_balance_catches_hallucinated_middle_row() -> None:
    """Row 2's running_balance is impossible given row 1 + row 2 amount."""
    stmt = _stmt(
        [
            Transaction(
                posted_date=date(2026, 1, 5),
                description="DEPOSIT 1",
                amount=Decimal("300.00"),
                running_balance=Decimal("1300.00"),
                source_page=1,
                source_line=1,
            ),
            Transaction(
                posted_date=date(2026, 1, 5),
                description="DEPOSIT 2",
                amount=Decimal("200.00"),
                # Hallucinated: should be 1500, claimed 9999
                running_balance=Decimal("9999.00"),
                source_page=1,
                source_line=2,
            ),
            Transaction(
                posted_date=date(2026, 1, 5),
                description="WITHDRAW",
                amount=Decimal("-200.00"),
                # Final row reverts to truth so day-end balance ties:
                running_balance=Decimal("1300.00"),
                source_page=1,
                source_line=3,
            ),
        ],
    )
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not result.passed
    assert any(
        f.startswith("reconciliation_failed_intraday") for f in result.failures
    ), result.failures


def test_intraday_balance_skips_chain_across_none_running_balance() -> None:
    """If a row has running_balance=None, the chain resets across it."""
    stmt = _stmt(
        [
            Transaction(
                posted_date=date(2026, 1, 5),
                description="DEPOSIT 1",
                amount=Decimal("300.00"),
                running_balance=Decimal("1300.00"),
                source_page=1,
                source_line=1,
            ),
            Transaction(
                posted_date=date(2026, 1, 5),
                description="MID NO BALANCE",
                amount=Decimal("100.00"),
                running_balance=None,
                source_page=1,
                source_line=2,
            ),
            Transaction(
                posted_date=date(2026, 1, 5),
                description="DEPOSIT 3",
                amount=Decimal("100.00"),
                # After the None gap; chain only verifies running pairs.
                # This one's balance can be anything: 1500 (truth) or
                # any other value — the prior None should reset.
                running_balance=Decimal("1500.00"),
                source_page=1,
                source_line=3,
            ),
        ],
    )
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert result.passed, f"None gap resets chain; failures={result.failures!r}"


def test_row_count_parity_hard_fails_small_statement() -> None:
    """printed=50, extracted=45 → diff=5, tolerance=max(3, 1)=3 → fail."""
    stmt = _stmt(
        [
            Transaction(
                posted_date=date(2026, 1, 5),
                description="ROW",
                amount=Decimal("10.00"),
                running_balance=Decimal("1010.00"),
                source_page=1,
                source_line=1,
            ),
        ],
        beginning=Decimal("1000.00"),
        ending=Decimal("1010.00"),
        deposits=Decimal("10.00"),
        withdrawals=Decimal("0.00"),
        printed_count=50,
    )
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not result.passed
    assert any(
        f.startswith("extraction_row_count_mismatch") for f in result.failures
    ), result.failures


def test_row_count_parity_passes_large_statement_small_diff() -> None:
    """printed=800, extracted=795 → diff=5, tolerance=max(3, 16)=16 → pass."""
    txns = [
        Transaction(
            posted_date=date(2026, 1, 5),
            description=f"ROW {i}",
            amount=Decimal("1.00"),
            running_balance=Decimal("1000.00") + Decimal(str(i + 1)),
            source_page=1 + i // 50,
            source_line=1 + (i % 50),
        )
        for i in range(795)
    ]
    stmt = _stmt(
        txns,
        beginning=Decimal("1000.00"),
        ending=Decimal("1795.00"),
        deposits=Decimal("795.00"),
        withdrawals=Decimal("0.00"),
        printed_count=800,
    )
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    # Should NOT trip the row-count gate. Other gates (period reconciliation
    # etc.) are tied via the helper.
    assert not any(
        f.startswith("extraction_row_count_mismatch") for f in result.failures
    ), f"5/800 should pass; failures={result.failures!r}"


def test_row_count_parity_no_op_when_printed_count_missing() -> None:
    """If the bank didn't print a count, no parity check runs (backwards-compat)."""
    stmt = _stmt(
        [
            Transaction(
                posted_date=date(2026, 1, 5),
                description="ROW",
                amount=Decimal("10.00"),
                running_balance=Decimal("1010.00"),
                source_page=1,
                source_line=1,
            ),
        ],
        beginning=Decimal("1000.00"),
        ending=Decimal("1010.00"),
        deposits=Decimal("10.00"),
        withdrawals=Decimal("0.00"),
        printed_count=None,
    )
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert result.passed, f"missing printed_count should be a no-op; failures={result.failures!r}"
