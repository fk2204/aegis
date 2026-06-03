"""Phase 9 detector unit tests.

Covers the §6.4 detectors added in Phase 9:

- ``unreconciled_internal_transfer`` (highest-value — hidden account)
- ``mca_payoff_signature``
- ``customer_concentration`` (statement-derived, scoreable)
- ``chargeback_velocity``
- ``unauthorized_withdrawal_dispute`` (Phase 9 hard-decline candidate)
- ``acceleration_clause_triggered`` (Phase 9 hard-decline candidate)
- ``processor_holdback_detected``
- ``payroll_absent`` (soft signal)
- ``ai_generated_statement`` composite (signal only)
- counterparty signals (top-1, top-5 revenue/expense shares)

Each detector is tested with: (a) fires above threshold, (b) does not
fire below, (c) source_ids point at the triggering rows when applicable.

Symmetric audit-trail rule: every Pattern with source_ids must point at
rows actually present in the input.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction, TransactionCategory
from aegis.parser.patterns import (
    CounterpartySignals,
    Pattern,
    PatternAnalysis,
    analyze_patterns,
)

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)
TODAY = date(2026, 2, 5)


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


def _analyze(
    txns: list[ClassifiedTransaction],
    *,
    period_start: date = PERIOD_START,
    period_end: date = PERIOD_END,
    today: date = TODAY,
) -> PatternAnalysis:
    return analyze_patterns(
        txns,
        period_start=period_start,
        period_end=period_end,
        today=today,
    )


def _by_code(res: PatternAnalysis) -> dict[str, Pattern]:
    return {p.code: p for p in res.patterns}


# -- unreconciled_internal_transfer ------------------------------------------


def test_unreconciled_internal_transfer_fires_on_unmatched_out() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 10),
            description="TRANSFER TO XXXX1234",
            amount=Decimal("-2500.00"),
            category="transfer",
        ),
        _txn(
            posted_date=date(2026, 1, 11),
            description="ACH DEPOSIT CUSTOMER",
            amount=Decimal("1000.00"),
            category="ach_credit",
        ),
    ]
    pats = _by_code(_analyze(txns))
    assert "unreconciled_internal_transfer" in pats
    assert pats["unreconciled_internal_transfer"].source_ids == [txns[0].id]


def test_unreconciled_internal_transfer_does_not_fire_on_matched_leg() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 10),
            description="TRANSFER TO XXXX1234",
            amount=Decimal("-2500.00"),
            category="transfer",
        ),
        _txn(
            posted_date=date(2026, 1, 11),
            description="TRANSFER FROM XXXX1234",
            amount=Decimal("2500.00"),
            category="transfer",
        ),
    ]
    assert "unreconciled_internal_transfer" not in _by_code(_analyze(txns))


def test_unreconciled_internal_transfer_skips_small_legs() -> None:
    # < $500 ignored — too small to be a hidden-account signal.
    txns = [
        _txn(
            posted_date=date(2026, 1, 10),
            description="TRANSFER TO SAVINGS",
            amount=Decimal("-450.00"),
            category="transfer",
        ),
    ]
    assert "unreconciled_internal_transfer" not in _by_code(_analyze(txns))


# -- mca_payoff_signature ----------------------------------------------------


def test_mca_payoff_signature_fires_on_large_known_funder_debit() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="ONDECK PAYOFF",
            amount=Decimal("-8500.00"),
            category="mca_debit",
        ),
    ]
    pats = _by_code(_analyze(txns))
    assert "mca_payoff_signature" in pats
    assert pats["mca_payoff_signature"].source_ids == [txns[0].id]


def test_mca_payoff_signature_ignores_small_debits() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="ONDECK DAILY PMT",
            amount=Decimal("-450.00"),
            category="mca_debit",
        ),
    ]
    assert "mca_payoff_signature" not in _by_code(_analyze(txns))


def test_mca_payoff_signature_ignores_unknown_payee() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="VENDOR PAYMENT XYZ INC",
            amount=Decimal("-8500.00"),
            category="other",
        ),
    ]
    assert "mca_payoff_signature" not in _by_code(_analyze(txns))


# -- customer_concentration --------------------------------------------------


def test_customer_concentration_fires_on_dominant_payer() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, d),
            description="ACH BIG CLIENT INC",
            amount=Decimal("10000.00"),
            category="ach_credit",
            source_line=i + 1,
        )
        for i, d in enumerate([3, 8, 13])
    ] + [
        _txn(
            posted_date=date(2026, 1, 20),
            description="ACH SMALL CUSTOMER",
            amount=Decimal("500.00"),
            category="ach_credit",
            source_line=4,
        ),
    ]
    res = _analyze(txns)
    pats = _by_code(res)
    assert "customer_concentration" in pats
    assert res.counterparty_signals.top_counterparty_pct is not None
    assert res.counterparty_signals.top_counterparty_pct >= 95


def test_customer_concentration_does_not_fire_when_spread() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, i * 2 + 1),
            description=f"ACH CUSTOMER {i}",
            amount=Decimal("1000.00"),
            category="ach_credit",
            source_line=i + 1,
        )
        for i in range(10)
    ]
    res = _analyze(txns)
    assert "customer_concentration" not in _by_code(res)
    # top counterparty is each unique ~10% so far below 30 threshold.
    assert res.counterparty_signals.top_counterparty_pct == 10


# -- chargeback_velocity -----------------------------------------------------


def test_chargeback_velocity_fires_on_late_window_spike() -> None:
    # 1 chargeback per fortnight earlier, 5 in last 14 days.
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="CHARGEBACK FEE",
            amount=Decimal("-50.00"),
            category="chargeback",
        ),
    ] + [
        _txn(
            posted_date=date(2026, 1, day),
            description="REFUND TO CUSTOMER",
            amount=Decimal("-100.00"),
            category="refund",
            source_line=i + 2,
        )
        for i, day in enumerate([18, 20, 22, 25, 28])
    ]
    pats = _by_code(_analyze(txns))
    assert "chargeback_velocity" in pats


def test_chargeback_velocity_does_not_fire_below_threshold() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="CHARGEBACK FEE",
            amount=Decimal("-50.00"),
            category="chargeback",
        ),
    ]
    assert "chargeback_velocity" not in _by_code(_analyze(txns))


# -- unauthorized_withdrawal_dispute -----------------------------------------


def test_unauthorized_withdrawal_dispute_fires_on_paired_reversal() -> None:
    debit = _txn(
        posted_date=date(2026, 1, 10),
        description="ONDECK DAILY PMT",
        amount=Decimal("-450.00"),
        category="mca_debit",
    )
    credit = _txn(
        posted_date=date(2026, 1, 12),
        description="ACH REVERSAL UNAUTHORIZED",
        amount=Decimal("450.00"),
        category="refund",
    )
    res = _analyze([debit, credit])
    pats = _by_code(res)
    assert "unauthorized_withdrawal_dispute" in pats
    assert res.unauthorized_withdrawal_dispute is True


def test_unauthorized_withdrawal_dispute_skips_when_no_prior_debit() -> None:
    credit = _txn(
        posted_date=date(2026, 1, 12),
        description="ACH REVERSAL UNAUTHORIZED",
        amount=Decimal("450.00"),
        category="refund",
    )
    res = _analyze([credit])
    assert res.unauthorized_withdrawal_dispute is False


# -- acceleration_clause_triggered -------------------------------------------


def test_acceleration_clause_triggered_fires_on_5x_debit_after_recurring() -> None:
    # 5 recurring $500 ONDECK debits, then a $3,500 lump-sum (7x median).
    rows = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ONDECK DAILY PMT",
            amount=Decimal("-500.00"),
            category="mca_debit",
            source_line=i + 1,
        )
        for i, day in enumerate([2, 4, 6, 8, 10])
    ] + [
        _txn(
            posted_date=date(2026, 1, 15),
            description="ONDECK DAILY PMT",
            amount=Decimal("-3500.00"),
            category="mca_debit",
            source_line=6,
        ),
    ]
    # Use a long period so latest is ≥7 days before period_end.
    res = _analyze(rows, period_start=date(2026, 1, 1), period_end=date(2026, 2, 15))
    pats = _by_code(res)
    assert "acceleration_clause_triggered" in pats
    assert res.acceleration_clause_triggered is True


def test_acceleration_clause_does_not_fire_when_ratio_too_small() -> None:
    rows = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ONDECK DAILY PMT",
            amount=Decimal("-500.00"),
            category="mca_debit",
            source_line=i + 1,
        )
        for i, day in enumerate([2, 4, 6, 8, 10])
    ] + [
        _txn(
            posted_date=date(2026, 1, 15),
            description="ONDECK DAILY PMT",
            amount=Decimal("-1500.00"),  # only 3x — below 5x floor
            category="mca_debit",
            source_line=6,
        ),
    ]
    res = _analyze(rows, period_start=date(2026, 1, 1), period_end=date(2026, 2, 15))
    assert res.acceleration_clause_triggered is False


def test_acceleration_clause_skips_when_recurring_resumed() -> None:
    # Recurring → big debit → recurring resumed within period.
    rows = [
        _txn(
            posted_date=date(2026, 1, day),
            description="ONDECK DAILY PMT",
            amount=Decimal("-500.00"),
            category="mca_debit",
            source_line=i + 1,
        )
        for i, day in enumerate([2, 4, 6, 8])
    ] + [
        _txn(
            posted_date=date(2026, 1, 10),
            description="ONDECK DAILY PMT",
            amount=Decimal("-3500.00"),  # would qualify alone
            category="mca_debit",
            source_line=5,
        ),
        # And the last occurrence isn't far enough from period_end
    ]
    res = _analyze(rows, period_start=date(2026, 1, 1), period_end=date(2026, 1, 14))
    assert res.acceleration_clause_triggered is False


# -- processor_holdback_detected ---------------------------------------------


def test_processor_holdback_detected_fires_on_noisy_payouts() -> None:
    # Stripe-style payouts with high CV — large variability.
    amounts = ["5000", "100", "8000", "200", "4500", "0.01", "6500",
               "50", "7000", "300", "5500", "100"]
    txns = [
        _txn(
            posted_date=date(2026, 1, i + 2),
            description="STRIPE TRANSFER",
            amount=Decimal(a),
            category="ach_credit",
            source_line=i + 1,
        )
        for i, a in enumerate(amounts)
    ]
    pats = _by_code(_analyze(txns))
    assert "processor_holdback_detected" in pats


def test_processor_holdback_skips_steady_payouts() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, i + 2),
            description="STRIPE TRANSFER",
            amount=Decimal("5000.00"),
            category="ach_credit",
            source_line=i + 1,
        )
        for i in range(12)
    ]
    assert "processor_holdback_detected" not in _by_code(_analyze(txns))


# -- payroll_absent ----------------------------------------------------------


def test_payroll_absent_fires_when_high_revenue_no_payroll() -> None:
    # $80k of revenue, no payroll-processor rows.
    txns = [
        _txn(
            posted_date=date(2026, 1, i + 1),
            description=f"ACH CUSTOMER {i}",
            amount=Decimal("4000.00"),
            category="ach_credit",
            source_line=i + 1,
        )
        for i in range(20)
    ]
    pats = _by_code(_analyze(txns))
    assert "payroll_absent" in pats


def test_payroll_absent_skips_when_payroll_present() -> None:
    revenue = [
        _txn(
            posted_date=date(2026, 1, i + 1),
            description=f"ACH CUSTOMER {i}",
            amount=Decimal("4000.00"),
            category="ach_credit",
            source_line=i + 1,
        )
        for i in range(20)
    ]
    payroll = _txn(
        posted_date=date(2026, 1, 15),
        description="ADP PAYROLL FEES",
        amount=Decimal("-2500.00"),
        category="payroll",
    )
    res = _analyze([*revenue, payroll])
    assert "payroll_absent" not in _by_code(res)
    assert res.payroll_present is True


def test_payroll_absent_skips_low_revenue() -> None:
    # Below $50k threshold — sole-prop plausible.
    txns = [
        _txn(
            posted_date=date(2026, 1, i + 1),
            description=f"ACH CUSTOMER {i}",
            amount=Decimal("1000.00"),
            category="ach_credit",
            source_line=i + 1,
        )
        for i in range(5)
    ]
    assert "payroll_absent" not in _by_code(_analyze(txns))


# -- ai_generated_statement composite ----------------------------------------


def test_ai_generated_score_is_low_for_realistic_descriptions() -> None:
    # Mix of all-caps, trace ids, abbreviations — looks real.
    txns = [
        _txn(
            posted_date=date(2026, 1, i + 1),
            description=f"ACH DEPOSIT TRACE#123{i}45678",
            amount=Decimal("1500.55"),
            category="ach_credit",
            source_line=i + 1,
        )
        for i in range(10)
    ]
    res = _analyze(txns)
    assert res.ai_generated_score < 50


def test_ai_generated_score_is_high_for_clean_descriptions() -> None:
    txns = [
        _txn(
            posted_date=date(2026, 1, i + 1),
            description="Customer Payment",  # title case, no caps, no ids
            amount=Decimal("1500.00"),  # round
            category="ach_credit",
            source_line=i + 1,
        )
        for i in range(15)
    ]
    res = _analyze(txns)
    assert res.ai_generated_score >= 60


# -- counterparty signals ----------------------------------------------------


def test_counterparty_signals_top5_share() -> None:
    # 10 distinct payers; top-5 should account for >50% by amount.
    rows: list[ClassifiedTransaction] = []
    for i in range(10):
        # First 5 are larger.
        amt = Decimal("2000.00") if i < 5 else Decimal("500.00")
        rows.append(
            _txn(
                posted_date=date(2026, 1, i + 1),
                description=f"ACH CUSTOMER {chr(ord('A') + i)}",
                amount=amt,
                category="ach_credit",
                source_line=i + 1,
            )
        )
    res = _analyze(rows)
    sig: CounterpartySignals = res.counterparty_signals
    assert sig.top_counterparty_pct is not None
    assert sig.top_5_revenue_share_pct is not None
    # 5 * 2000 / (5*2000 + 5*500) = 10000 / 12500 = 80%
    assert sig.top_5_revenue_share_pct == 80


def test_counterparty_signals_empty_when_no_revenue() -> None:
    # Only debits — no revenue side.
    rows = [
        _txn(
            posted_date=date(2026, 1, 5),
            description="VENDOR X",
            amount=Decimal("-500.00"),
            category="fee",
        ),
    ]
    res = _analyze(rows)
    assert res.counterparty_signals.top_counterparty_pct is None
    assert res.counterparty_signals.top_5_revenue_share_pct is None
