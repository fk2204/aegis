"""Sign-aware revenue aggregation in `aegis.parser.aggregate._true_revenue`.

Regression test for the VU Development bug shipped 2026-06-03: real
$450K/month merchant booked $24K / -$40K / $22K / $38K per statement
because intra-bank owner transfers (debits) were subtracted from
revenue. Fix: only positive entries in `_REVENUE_EXCLUDED` reduce
revenue; debit-side transfers and chargebacks never contributed to
the positive deposit stream and must not be re-subtracted.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.parser.aggregate import aggregate
from aegis.parser.models import ClassifiedTransaction, TransactionCategory

PERIOD_START = date(2026, 3, 1)
PERIOD_END = date(2026, 3, 31)
BEGINNING = Decimal("100000.00")


def _txn(
    *,
    amount: Decimal,
    category: TransactionCategory,
    description: str = "row",
    posted: date = date(2026, 3, 15),
    running_balance: Decimal | None = None,
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted,
        description=description,
        amount=amount,
        running_balance=running_balance,
        source_page=1,
        source_line=1,
        category=category,
        classification_confidence=95,
    )


def _true_revenue(txns: list[ClassifiedTransaction]) -> Decimal:
    """Run the public aggregate() and return its true_revenue."""
    return aggregate(
        txns,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        beginning_balance=BEGINNING,
    ).aggregates.true_revenue.value


def test_positive_deposits_sum_in() -> None:
    txns = [
        _txn(amount=Decimal("10000.00"), category="deposit"),
        _txn(amount=Decimal("5000.00"), category="ach_credit"),
        _txn(amount=Decimal("2000.00"), category="wire_in"),
        _txn(amount=Decimal("250.00"), category="refund"),
    ]
    assert _true_revenue(txns) == Decimal("17250.00")


def test_outbound_transfer_does_not_reduce_revenue() -> None:
    """VU regression: -$300K owner transfer to CHK 7722 must NOT cut revenue.

    Before the fix this returned -$290,000 (deposit minus abs(transfer)).
    """
    txns = [
        _txn(amount=Decimal("10000.00"), category="deposit"),
        _txn(
            amount=Decimal("-300000.00"),
            category="transfer",
            description="Online Banking transfer to CHK 7722",
        ),
    ]
    assert _true_revenue(txns) == Decimal("10000.00")


def test_inbound_transfer_is_subtracted() -> None:
    """Owner-deposit transfers (positive sign in `transfer` category) are
    not real revenue and must be netted out."""
    txns = [
        _txn(amount=Decimal("10000.00"), category="deposit"),
        _txn(
            amount=Decimal("3000.00"),
            category="transfer",
            description="Online Banking transfer from CHK 7722",
        ),
    ]
    assert _true_revenue(txns) == Decimal("7000.00")


def test_chargeback_credit_subtracts() -> None:
    """A +$X chargeback (credit-side reversal) cancels a prior deposit."""
    txns = [
        _txn(amount=Decimal("10000.00"), category="deposit"),
        _txn(amount=Decimal("500.00"), category="chargeback"),
    ]
    assert _true_revenue(txns) == Decimal("9500.00")


def test_chargeback_debit_does_not_reduce_revenue() -> None:
    """A -$X chargeback (debit-side fee) is already absent from revenue;
    re-subtracting its absolute value double-counts."""
    txns = [
        _txn(amount=Decimal("10000.00"), category="deposit"),
        _txn(amount=Decimal("-500.00"), category="chargeback"),
    ]
    assert _true_revenue(txns) == Decimal("10000.00")


def test_vu_march_shape() -> None:
    """End-to-end shape of VU's March statement: large wire credits PLUS
    large outbound transfers should produce revenue ≈ sum of wire credits,
    not (wire credits - abs(transfers))."""
    txns = [
        _txn(amount=Decimal("124950.00"), category="wire_in"),
        _txn(amount=Decimal("124775.00"), category="wire_in"),
        _txn(amount=Decimal("100005.00"), category="wire_in"),
        _txn(amount=Decimal("58025.00"), category="wire_in"),
        _txn(amount=Decimal("13950.26"), category="wire_in"),
        _txn(amount=Decimal("-164916.25"), category="transfer"),
        _txn(amount=Decimal("-161692.37"), category="transfer"),
        _txn(amount=Decimal("-125792.37"), category="transfer"),
    ]
    expected = Decimal("421705.26")  # sum of the wire credits only
    assert _true_revenue(txns) == expected


def test_zero_amount_in_excluded_does_not_pollute_sources() -> None:
    """A 0-amount transfer/chargeback (rare but possible if classifier
    returns a row with no value) must not enter the revenue sources list
    via a no-op subtraction."""
    txns = [
        _txn(amount=Decimal("1000.00"), category="deposit"),
        _txn(amount=Decimal("0.00"), category="transfer"),
    ]
    result = aggregate(
        txns,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        beginning_balance=BEGINNING,
    )
    sources = result.aggregates.true_revenue.source_ids
    assert len(sources) == 1
    assert sources[0] == txns[0].id
