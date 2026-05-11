"""Tests for the new velocity-based pattern detectors.

`_deposit_velocity_spike` and `_withdrawal_acceleration` catch different
flavors than the existing preloan/paydown detectors:
  - velocity_spike fires on COUNT of deposits per window, not dollars.
  - withdrawal_acceleration fires on MCA-debit COUNT in trailing week,
    not amount magnitude (paydown detects descending amounts).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction, TransactionCategory
from aegis.parser.patterns import analyze_patterns


def _txn(
    *,
    posted_date: date,
    description: str,
    amount: Decimal,
    category: TransactionCategory = "deposit",
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        running_balance=None,
        source_page=1,
        source_line=1,
        category=category,
        classification_confidence=95,
    )


def test_deposit_velocity_spike_fires_on_count_clustering() -> None:
    """30-day period: 1 deposit/week for 3 weeks, then 15 deposits in week 4 = spike."""
    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 30)
    today = date(2026, 2, 5)

    txns: list[ClassifiedTransaction] = []
    # Sparse baseline: 1 deposit on day 3, day 10, day 17.
    for day in (3, 10, 17):
        txns.append(
            _txn(
                posted_date=period_start + timedelta(days=day - 1),
                description=f"CUSTOMER PAYMENT {day}",
                amount=Decimal("1000.00"),
            )
        )
    # Cluster: 15 deposits in last week.
    for day in range(24, 30):
        for n in range(3):
            txns.append(
                _txn(
                    posted_date=period_start + timedelta(days=day - 1),
                    description=f"CUSTOMER PAYMENT {day}-{n}",
                    amount=Decimal("1000.00"),
                )
            )

    res = analyze_patterns(
        txns,
        period_start=period_start,
        period_end=period_end,
        today=today,
    )
    codes = {p.code for p in res.patterns}
    assert "deposit_velocity_spike" in codes, codes


def test_deposit_velocity_spike_silent_on_steady_stream() -> None:
    """30-day period, ~1 deposit every 2 days, no clustering → no spike."""
    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 30)
    today = date(2026, 2, 5)

    txns = [
        _txn(
            posted_date=period_start + timedelta(days=day),
            description=f"CUSTOMER {day}",
            amount=Decimal(str(1000 + day * 17)),
        )
        for day in range(0, 30, 2)
    ]
    res = analyze_patterns(
        txns, period_start=period_start, period_end=period_end, today=today
    )
    codes = {p.code for p in res.patterns}
    assert "deposit_velocity_spike" not in codes


def test_withdrawal_acceleration_fires_on_mca_count_spike() -> None:
    """30-day period: 1 MCA debit/week for 3 weeks, then 6 in last week."""
    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 30)
    today = date(2026, 2, 5)

    txns: list[ClassifiedTransaction] = []
    for day in (3, 10, 17):
        txns.append(
            _txn(
                posted_date=period_start + timedelta(days=day - 1),
                description="ONDECK ACH DAILY",
                amount=Decimal("-500.00"),
                category="mca_debit",
            )
        )
    for day in (24, 25, 26, 27, 28, 29):
        txns.append(
            _txn(
                posted_date=period_start + timedelta(days=day - 1),
                description="ONDECK ACH DAILY",
                amount=Decimal("-500.00"),
                category="mca_debit",
            )
        )
    res = analyze_patterns(
        txns, period_start=period_start, period_end=period_end, today=today
    )
    codes = {p.code for p in res.patterns}
    assert "withdrawal_acceleration" in codes, codes


def test_withdrawal_acceleration_silent_on_flat_mca_pattern() -> None:
    """4 evenly-spaced MCA debits → no acceleration."""
    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 30)
    today = date(2026, 2, 5)

    txns = [
        _txn(
            posted_date=period_start + timedelta(days=day - 1),
            description="ONDECK ACH DAILY",
            amount=Decimal("-500.00"),
            category="mca_debit",
        )
        for day in (5, 12, 19, 26)
    ]
    res = analyze_patterns(
        txns, period_start=period_start, period_end=period_end, today=today
    )
    codes = {p.code for p in res.patterns}
    assert "withdrawal_acceleration" not in codes


def test_velocity_detectors_silent_on_short_statement() -> None:
    """Both velocity detectors require >= 21-day statement; <21 = bail."""
    period_start = date(2026, 1, 1)
    period_end = date(2026, 1, 15)
    today = date(2026, 2, 5)

    txns = [
        _txn(
            posted_date=period_start + timedelta(days=day),
            description="DEPOSIT",
            amount=Decimal("1000.00"),
        )
        for day in range(15)
    ]
    res = analyze_patterns(
        txns, period_start=period_start, period_end=period_end, today=today
    )
    codes = {p.code for p in res.patterns}
    assert "deposit_velocity_spike" not in codes
    assert "withdrawal_acceleration" not in codes
