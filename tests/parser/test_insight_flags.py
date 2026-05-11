"""Tests for the new aggregate-level insight flags.

These are emitted via ``AggregateResult.flags`` and surface on the
merchant detail page as ``[AGGREGATE] …`` flags. They don't trigger
gates (those run separately via patterns); they're operator context.

  - top_counterparty_concentration:{pct}%_({payee})
  - payroll_cadence:{cadence}[_{pct}%_of_revenue]
  - nsf_on_negative_days:{overlap}_of_{total}
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from aegis.parser.aggregate import aggregate
from aegis.parser.models import ClassifiedTransaction, TransactionCategory


def _txn(
    *,
    posted_date: date,
    description: str,
    amount: Decimal,
    category: TransactionCategory = "deposit",
    running_balance: Decimal | None = None,
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        running_balance=running_balance,
        source_page=1,
        source_line=1,
        category=category,
        classification_confidence=95,
    )


PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)


def _flags(transactions: list[ClassifiedTransaction]) -> list[str]:
    return aggregate(
        transactions,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        beginning_balance=Decimal("5000.00"),
    ).flags


def test_top_counterparty_concentration_fires_above_minimum_payee_count() -> None:
    """3+ distinct payees, ONE_BIG_CUSTOMER at 60% share → emits concentration flag."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 1),
            description="ONE BIG CUSTOMER",
            amount=Decimal("6000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 5),
            description="SMALL CUSTOMER A",
            amount=Decimal("2000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 10),
            description="SMALL CUSTOMER B",
            amount=Decimal("2000.00"),
        ),
    ]
    flags = _flags(txns)
    assert any(
        f.startswith("top_counterparty_concentration:60%") for f in flags
    ), flags


def test_top_counterparty_silent_with_too_few_payees() -> None:
    """Only 2 distinct payees → no concentration flag (not enough baseline)."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 1),
            description="CUSTOMER A",
            amount=Decimal("5000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 5),
            description="CUSTOMER B",
            amount=Decimal("5000.00"),
        ),
    ]
    flags = _flags(txns)
    assert not any(
        f.startswith("top_counterparty_concentration") for f in flags
    ), flags


def test_payroll_cadence_weekly() -> None:
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=d),
            description="ADP PAYROLL",
            amount=Decimal("-3000.00"),
            category="payroll",
        )
        for d in (0, 7, 14, 21)
    ]
    flags = _flags(txns)
    assert any(f.startswith("payroll_cadence:weekly") for f in flags), flags


def test_payroll_cadence_biweekly() -> None:
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=d),
            description="ADP PAYROLL",
            amount=Decimal("-6000.00"),
            category="payroll",
        )
        for d in (0, 14, 28)
    ]
    flags = _flags(txns)
    assert any(f.startswith("payroll_cadence:biweekly") for f in flags), flags


def test_payroll_cadence_monthly() -> None:
    long_end = PERIOD_START + timedelta(days=45)
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=d),
            description="ADP PAYROLL",
            amount=Decimal("-12000.00"),
            category="payroll",
        )
        for d in (1, 31)
    ]
    flags = aggregate(
        txns,
        period_start=PERIOD_START,
        period_end=long_end,
        beginning_balance=Decimal("100000.00"),
    ).flags
    assert any(f.startswith("payroll_cadence:monthly") for f in flags), flags


def test_payroll_cadence_silent_when_no_payroll() -> None:
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=d),
            description=f"DEPOSIT {d}",
            amount=Decimal("1000.00"),
        )
        for d in (1, 5, 10)
    ]
    flags = _flags(txns)
    assert not any(f.startswith("payroll_cadence") for f in flags), flags


def test_nsf_on_negative_days_reports_overlap() -> None:
    """One NSF on a day the balance is in the red → "1_of_1" flag."""
    txns = [
        _txn(
            posted_date=date(2026, 1, 2),
            description="BIG WD",
            amount=Decimal("-10000.00"),
            running_balance=Decimal("-5000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 2),
            description="NSF FEE",
            amount=Decimal("-35.00"),
            running_balance=Decimal("-5035.00"),
            category="nsf_fee",
        ),
    ]
    flags = _flags(txns)
    assert any(f.startswith("nsf_on_negative_days:1_of_1") for f in flags), flags


def test_nsf_on_negative_days_silent_without_nsf() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 2),
            description="DEPOSIT",
            amount=Decimal("1000.00"),
        ),
    ]
    flags = _flags(txns)
    assert not any(f.startswith("nsf_on_negative_days") for f in flags), flags
