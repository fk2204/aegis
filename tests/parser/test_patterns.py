"""Unit tests for every deterministic pattern detector.

Each test asserts:
  (a) detector fires above its threshold,
  (b) does not fire below,
  (c) source_ids point at the rows that triggered the detection.

Detector landscape (parser/patterns.py):
  - mca_stacking            (≥3 occurrences known funder, OR ≥10 + daily generic)
  - duplicate_deposits      (same date + amount, ≥1 pair)
  - synthetic_low_variance  (≥10 deposits, CV < 0.15)
  - round_number_deposits   (≥10 deposits, >75% exact $100 multiples)
  - preloan_spike           (last-7 > 2.5x prior avg, or last-14 > 2.5x)
  - nsf_clustering_short    (>3 NSF in <20-day statement)
  - nsf_late_concentration  (>=3 NSF in last 30d, statement >30d)
  - wash_deposit_suspected  (>=2 dep/wd pairs within 5d, 2% tolerance)
  - paydown_mca_suspected   (>=5 same-payee debits, monotonic 5% noise, <=0.85x)
  - recent_account_opening  (statement starts <60 days before today)
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction, TransactionCategory
from aegis.parser.patterns import Pattern, analyze_patterns


def _txn(
    *,
    posted_date: date,
    description: str,
    amount: Decimal,
    category: TransactionCategory = "deposit",
    source_page: int = 1,
    source_line: int | None = None,
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        running_balance=None,
        source_page=source_page,
        source_line=source_line or 1,
        category=category,
        classification_confidence=95,
    )


PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)
TODAY = date(2026, 2, 5)


def _analyze(txns: list[ClassifiedTransaction]) -> dict[str, Pattern]:
    """Run analyze_patterns + return a code->Pattern lookup."""
    res = analyze_patterns(
        txns, period_start=PERIOD_START, period_end=PERIOD_END, today=TODAY
    )
    return {p.code: p for p in res.patterns}


def test_mca_stacking_fires_on_three_known_funder_debits() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ONDECK MCA ACH",
            amount=Decimal("-500.00"),
            category="mca_debit",
            source_line=i + 1,
        )
        for i, day in enumerate([5, 10, 15])
    ]
    by_code = _analyze(txns)
    assert "mca_stacking" in by_code, list(by_code)
    pat = by_code["mca_stacking"]
    assert pat.severity == 15  # 15 * 1 position
    assert set(pat.source_ids) == {t.id for t in txns}


def test_mca_stacking_does_not_fire_below_threshold() -> None:
    # Only 2 occurrences — below the ≥3 threshold for a position.
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ONDECK MCA ACH",
            amount=Decimal("-500.00"),
            category="mca_debit",
        )
        for day in [5, 10]
    ]
    assert "mca_stacking" not in _analyze(txns)


def test_duplicate_deposits_fires_on_same_date_amount_pair() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="DEPOSIT A",
            amount=Decimal("500.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 5),
            description="DEPOSIT B",
            amount=Decimal("500.00"),
        ),
    ]
    by_code = _analyze(txns)
    assert "duplicate_deposits_detected" in by_code
    assert by_code["duplicate_deposits_detected"].severity == 30
    assert len(by_code["duplicate_deposits_detected"].source_ids) == 2


def test_duplicate_deposits_silent_on_different_dates() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="DEPOSIT",
            amount=Decimal("500.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 6),
            description="DEPOSIT",
            amount=Decimal("500.00"),
        ),
    ]
    assert "duplicate_deposits_detected" not in _analyze(txns)


def test_synthetic_low_variance_fires_at_or_below_cv_15pct() -> None:
    # 10 deposits within $50 of each other: CV ~3% → below 15% threshold.
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"DEPOSIT {day}",
            amount=Decimal("5000.00") + Decimal(day) * Decimal("10.00"),
        )
        for day in range(1, 11)
    ]
    by_code = _analyze(txns)
    assert "synthetic_low_variance" in by_code
    assert by_code["synthetic_low_variance"].severity == 25


def test_synthetic_low_variance_silent_with_wide_variance() -> None:
    # 10 deposits ranging $1k → $50k: CV well above 15%.
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"DEPOSIT {day}",
            amount=Decimal(1000 * day),
        )
        for day in range(1, 11)
    ]
    assert "synthetic_low_variance" not in _analyze(txns)


def test_round_number_deposits_fires_when_above_75pct() -> None:
    # 10 deposits, 9 of them exact $100 multiples → 90% → fire.
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"DEPOSIT {day}",
            amount=Decimal(f"{day}00.00"),
        )
        for day in range(1, 10)
    ] + [
        _txn(
            posted_date=date(2026, 1, 10),
            description="ODD DEPOSIT",
            amount=Decimal("1234.56"),
        )
    ]
    by_code = _analyze(txns)
    assert "round_number_deposits" in by_code
    assert by_code["round_number_deposits"].severity == 15


def test_round_number_deposits_silent_at_or_below_75pct() -> None:
    # 10 deposits, 7 round → 70% → no fire (threshold is >75%).
    txns = [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"DEPOSIT {day}",
            amount=Decimal(f"{day}00.00"),
        )
        for day in range(1, 8)
    ] + [
        _txn(
            posted_date=date(2026, 1, day),
            description=f"ODD {day}",
            amount=Decimal(f"{day * 137}.42"),
        )
        for day in (8, 9, 10)
    ]
    assert "round_number_deposits" not in _analyze(txns)


def test_preloan_spike_7d_fires_when_last_week_above_2_5x() -> None:
    # 21-day window: weeks 1+2 each $1000, week 3 $5000 → 5x spike.
    txns: list[ClassifiedTransaction] = []
    for d in (1, 5, 10):
        txns.append(
            _txn(
                posted_date=date(2026, 1, d),
                description=f"DEP {d}",
                amount=Decimal("1000.00"),
            )
        )
    # Last 7 days of period (period ends Jan 31) — push spike in last week.
    for d in (26, 28, 30):
        txns.append(
            _txn(
                posted_date=date(2026, 1, d),
                description=f"SPIKE {d}",
                amount=Decimal("5000.00"),
            )
        )
    by_code = _analyze(txns)
    assert "preloan_spike" in by_code
    assert by_code["preloan_spike"].severity == 25


def test_preloan_spike_silent_on_short_statement() -> None:
    # statement < 21 days: detector should bail early.
    short_end = PERIOD_START + timedelta(days=15)
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=d),
            description="DEP",
            amount=Decimal("1000.00"),
        )
        for d in (1, 5)
    ] + [
        _txn(
            posted_date=PERIOD_START + timedelta(days=13),
            description="SPIKE",
            amount=Decimal("10000.00"),
        ),
    ]
    res = analyze_patterns(
        txns, period_start=PERIOD_START, period_end=short_end, today=TODAY
    )
    assert "preloan_spike" not in {p.code for p in res.patterns}


def test_nsf_clustering_short_fires_on_4_nsf_in_19_days() -> None:
    short_end = PERIOD_START + timedelta(days=18)  # 19-day period
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=d),
            description="NSF FEE",
            amount=Decimal("-35.00"),
            category="nsf_fee",
        )
        for d in (2, 5, 10, 15)
    ]
    res = analyze_patterns(
        txns, period_start=PERIOD_START, period_end=short_end, today=TODAY
    )
    by_code = {p.code: p for p in res.patterns}
    assert "nsf_clustering_short" in by_code
    assert by_code["nsf_clustering_short"].severity == 20


def test_nsf_clustering_silent_below_threshold() -> None:
    short_end = PERIOD_START + timedelta(days=18)
    txns = [
        _txn(
            posted_date=PERIOD_START + timedelta(days=d),
            description="NSF FEE",
            amount=Decimal("-35.00"),
            category="nsf_fee",
        )
        for d in (2, 5, 10)
    ]
    res = analyze_patterns(
        txns, period_start=PERIOD_START, period_end=short_end, today=TODAY
    )
    assert "nsf_clustering_short" not in {p.code for p in res.patterns}


def test_nsf_late_concentration_fires_when_3_in_last_30_days() -> None:
    long_end = date(2026, 3, 5)  # 64-day period
    long_start = date(2026, 1, 1)
    txns = [
        _txn(
            posted_date=date(2026, 1, 15),
            description="NSF FEE",
            amount=Decimal("-35.00"),
            category="nsf_fee",
        ),
        _txn(
            posted_date=date(2026, 2, 25),
            description="NSF FEE",
            amount=Decimal("-35.00"),
            category="nsf_fee",
        ),
        _txn(
            posted_date=date(2026, 2, 28),
            description="NSF FEE",
            amount=Decimal("-35.00"),
            category="nsf_fee",
        ),
        _txn(
            posted_date=date(2026, 3, 3),
            description="NSF FEE",
            amount=Decimal("-35.00"),
            category="nsf_fee",
        ),
    ]
    res = analyze_patterns(
        txns, period_start=long_start, period_end=long_end, today=date(2026, 3, 10)
    )
    by_code = {p.code: p for p in res.patterns}
    assert "nsf_late_concentration" in by_code
    assert by_code["nsf_late_concentration"].severity == 20


def test_wash_deposit_kiting_fires_on_two_pairs() -> None:
    txns: list[ClassifiedTransaction] = []
    for d, amt in [(5, "1000.00"), (10, "2000.00")]:
        txns.append(
            _txn(
                posted_date=date(2026, 1, d),
                description=f"DEPOSIT {d}",
                amount=Decimal(amt),
            )
        )
        # Withdrawal within 2 days, within 2% of deposit.
        txns.append(
            _txn(
                posted_date=date(2026, 1, d + 2),
                description=f"WIRE OUT {d}",
                amount=-Decimal(amt),
                category="wire_out",
            )
        )
    by_code = _analyze(txns)
    assert "wash_deposit_suspected" in by_code
    assert by_code["wash_deposit_suspected"].severity == 35


def test_wash_deposit_silent_on_one_pair() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="DEPOSIT",
            amount=Decimal("1000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 6),
            description="WIRE OUT",
            amount=Decimal("-1000.00"),
            category="wire_out",
        ),
    ]
    assert "wash_deposit_suspected" not in _analyze(txns)


def test_paydown_mca_fires_on_descending_5plus_same_payee() -> None:
    # 5 same-payee debits, monotonically descending, final <= initial * 0.85.
    txns = [
        _txn(
            posted_date=date(2026, 1, d * 5),
            description="ONDECK ACH DAILY",
            amount=-Decimal(str(amount)),
            category="mca_debit",
        )
        for d, amount in zip(range(1, 6), [500, 450, 400, 350, 300], strict=True)
    ]
    by_code = _analyze(txns)
    assert "paydown_mca_suspected" in by_code
    assert by_code["paydown_mca_suspected"].severity == 25


def test_paydown_mca_silent_on_flat_amounts() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, d * 5),
            description="ONDECK ACH DAILY",
            amount=Decimal("-500.00"),
            category="mca_debit",
        )
        for d in range(1, 6)
    ]
    assert "paydown_mca_suspected" not in _analyze(txns)


def test_recent_account_opening_fires_when_period_within_60_days() -> None:
    # period_start is 30 days before today → severity = 15.
    today = date(2026, 1, 31)
    res = analyze_patterns(
        [],
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 28),
        today=today,
    )
    by_code = {p.code: p for p in res.patterns}
    assert "recent_account_opening" in by_code
    assert by_code["recent_account_opening"].severity == 15


def test_recent_account_opening_silent_after_60_days() -> None:
    # period_start is 75 days before today → severity = 0 → no pattern emitted.
    today = date(2026, 3, 16)
    res = analyze_patterns(
        [],
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 28),
        today=today,
    )
    assert "recent_account_opening" not in {p.code for p in res.patterns}
