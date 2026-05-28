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


def test_top_counterparty_concentration_never_exceeds_100pct() -> None:
    """Property: the concentration share is bounded at 100%.

    Regression for the "104% of net revenue" bug. The old denominator
    was ``true_revenue`` (positive deposits minus transfers and
    chargebacks); a dominant payer with even modest reversals would
    produce a ratio above 100%. The new denominator is gross deposits,
    so the ratio is bounded by definition.

    Fixture: top payee = $100k, two small payees = $1k each, plus a
    $4k chargeback. Under the old logic this would have rendered as
    ~104%; the new logic clamps to a sensible 98%.
    """
    txns = [
        _txn(
            posted_date=date(2026, 1, 3),
            description="DOMINANT CUSTOMER",
            amount=Decimal("100000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 8),
            description="SMALL CUSTOMER A",
            amount=Decimal("1000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 14),
            description="SMALL CUSTOMER B",
            amount=Decimal("1000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 20),
            description="CHARGEBACK FEE",
            amount=Decimal("-4000.00"),
            category="chargeback",
        ),
    ]
    flags = _flags(txns)
    concentration_flags = [
        f for f in flags if f.startswith("top_counterparty_concentration:")
    ]
    assert concentration_flags, flags
    # Extract NN from "top_counterparty_concentration:NN%_(...)"
    pct = int(concentration_flags[0].split(":")[1].split("%")[0])
    assert 0 <= pct <= 100, f"concentration must be bounded 0-100%, got {pct}"
    # Specifically: 100k / 102k gross = 98%.
    assert pct == 98, f"expected 98% under gross-revenue rule, got {pct}"


def test_top_counterparty_concentration_formula_matches_gross_definition() -> None:
    """Concentration = top_payee_gross / sum(all_positive_deposits).

    Hand-rolled fixture pinning the new formula: deposits 4000 + 3000 + 3000
    summed gross = 10000; top payee = 4000; concentration = 40%. A
    chargeback present in the same period must NOT affect this number —
    that's the bug we're regression-testing.
    """
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="CUSTOMER ALPHA",
            amount=Decimal("4000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 10),
            description="CUSTOMER BRAVO",
            amount=Decimal("3000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 15),
            description="CUSTOMER CHARLIE",
            amount=Decimal("3000.00"),
        ),
        # A chargeback in-period should not move the concentration ratio.
        _txn(
            posted_date=date(2026, 1, 22),
            description="DISPUTE REVERSAL",
            amount=Decimal("-500.00"),
            category="chargeback",
        ),
    ]
    flags = _flags(txns)
    assert any(
        f.startswith("top_counterparty_concentration:40%_(customer alpha)")
        for f in flags
    ), flags


def test_top_counterparty_concentration_label_strips_trailing_comma() -> None:
    """Real-world ACH descriptors arrive with trailing punctuation.

    "PAYWARD INTERACTIVE, INC ID 12345" was rendering as
    ``(payward interactive,)`` — trailing comma sneaking through the
    20-char display slice. ``_clean_payee_label`` strips it.
    """
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="PAYWARD INTERACTIVE, INC ID 12345",
            amount=Decimal("5000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 10),
            description="CUSTOMER ALPHA",
            amount=Decimal("2000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 15),
            description="CUSTOMER BRAVO",
            amount=Decimal("2000.00"),
        ),
    ]
    flags = _flags(txns)
    concentration_flags = [
        f for f in flags if f.startswith("top_counterparty_concentration:")
    ]
    assert concentration_flags, flags
    flag = concentration_flags[0]
    # Display label is the bit inside the parens.
    label = flag.split("(", 1)[1].rstrip(")")
    assert "," not in label, f"trailing comma in label: {label!r}"
    assert label == "payward interactive", f"unexpected label: {label!r}"


def test_top_counterparty_concentration_buckets_punctuation_variants_together() -> None:
    """Same payee with two trailing-punct variants must bucket together.

    "PAYWARD INTERACTIVE" and "PAYWARD INTERACTIVE, INC" should land in
    the SAME concentration bucket — workers reading "78%" do not want to
    see the same merchant split across two near-duplicate chips.
    """
    txns = [
        _txn(
            posted_date=date(2026, 1, 3),
            description="PAYWARD INTERACTIVE",
            amount=Decimal("4000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 8),
            description="PAYWARD INTERACTIVE, INC",
            amount=Decimal("4000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 12),
            description="OTHER CUSTOMER A",
            amount=Decimal("1000.00"),
        ),
        _txn(
            posted_date=date(2026, 1, 18),
            description="OTHER CUSTOMER B",
            amount=Decimal("1000.00"),
        ),
    ]
    flags = _flags(txns)
    # Bucketed share: 8000 / 10000 = 80%. If the bucket key were not
    # cleaned, the two Payward variants would be separate buckets and
    # neither alone (4000 / 10000 = 40%) would be the top.
    assert any(
        f.startswith("top_counterparty_concentration:80%_(payward interactive)")
        for f in flags
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
