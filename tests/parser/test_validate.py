"""Unit tests for the deterministic validation gate.

The gate is the firewall against AI hallucination — every failure mode
matters.
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


def _build_clean() -> ExtractedStatement:
    """Begin 1000 + dep 500 - wd 200 = 1300, both on day 1."""
    return ExtractedStatement(
        summary=StatementSummary(
            beginning_balance=Decimal("1000.00"),
            ending_balance=Decimal("1300.00"),
            deposit_total=Decimal("500.00"),
            withdrawal_total=Decimal("200.00"),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 20),  # 19 days
        ),
        transactions=[
            Transaction(
                posted_date=date(2026, 1, 5),
                description="DEPOSIT",
                amount=Decimal("500.00"),
                running_balance=Decimal("1500.00"),
                source_page=1,
                source_line=1,
            ),
            Transaction(
                posted_date=date(2026, 1, 5),
                description="WITHDRAW",
                amount=Decimal("-200.00"),
                running_balance=Decimal("1300.00"),
                source_page=1,
                source_line=2,
            ),
        ],
    )


def test_clean_statement_passes() -> None:
    result = validate_extraction(_build_clean(), today=date(2026, 1, 25))
    assert result.passed, f"clean stmt should pass; failures={result.failures}"


def test_period_too_short_fails() -> None:
    stmt = _build_clean()
    stmt.summary.period_end = date(2026, 1, 10)  # 9 days
    # adjust running balance check still aligns
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not result.passed
    assert any(f.startswith("invalid_period") for f in result.failures)


def test_period_too_long_fails() -> None:
    stmt = _build_clean()
    stmt.summary.period_end = date(2026, 3, 5)  # 63 days
    result = validate_extraction(stmt, today=date(2026, 3, 10))
    assert not result.passed
    assert any(f.startswith("invalid_period") for f in result.failures)


def test_future_dated_fails() -> None:
    stmt = _build_clean()
    result = validate_extraction(stmt, today=date(2025, 1, 1))
    assert not result.passed
    assert any(f.startswith("future_dated") for f in result.failures)


def test_period_reconciliation_failure() -> None:
    stmt = _build_clean()
    stmt.summary.ending_balance = Decimal("9999.00")  # off by miles
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not result.passed
    assert any(f.startswith("reconciliation_failed_period") for f in result.failures)


def test_listed_vs_summary_deposit_mismatch() -> None:
    stmt = _build_clean()
    stmt.summary.deposit_total = Decimal("9999.00")  # printed total wrong
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not result.passed
    assert any(
        f.startswith("reconciliation_failed_deposit_total") for f in result.failures
    )


def test_truncation_marks_failure() -> None:
    result = validate_extraction(_build_clean(), truncated=True, today=date(2026, 1, 25))
    assert not result.passed
    assert any(f.startswith("extraction_truncated") for f in result.failures)


def test_duplicate_source_lines_on_page_fail() -> None:
    stmt = _build_clean()
    # Two rows on page 1 with the same source_line should fail uniqueness.
    stmt.transactions[1].source_line = 1
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not result.passed
    assert any(f.startswith("missing_source_uniqueness") for f in result.failures)


def test_daily_balance_mismatch_fires() -> None:
    stmt = _build_clean()
    # Break the running balance: should be 1500 after deposit, set to 99999.
    stmt.transactions[1].running_balance = Decimal("99999.00")
    result = validate_extraction(stmt, today=date(2026, 1, 25))
    assert not result.passed
    assert any(f.startswith("daily_balance_mismatch") for f in result.failures)
