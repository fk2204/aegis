"""Shadow-mode validation: TD withdrawal-total coercion.

Per CORPUS_FINDINGS.md 2026-06-17, TD Convenience Checking prints
Electronic Payments and Service Charges as separate ACCOUNT SUMMARY
rows with no consolidated "Total Withdrawals" line. Bedrock extracts
Electronic Payments into ``summary.withdrawal_total``, but the
transaction stream contains the MSF service-charge row too — so
``listed_wd = printed_wd + msf``. The proper fix is a per-bank coercion
that adds the matched service-charge subtotal back to the printed
total before reconciliation.

This test suite locks the shadow-mode emission. Routing is unchanged
until the operator validates against a corpus + live shadow audit
window and flips coercion live via config (CLAUDE.md decision-boundary
discipline).
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
    td_service_charge_total,
    validate_extraction,
)


def _txn(
    day: int,
    amount: str,
    *,
    description: str = "TRANSACTION",
    source_line: int = 1,
) -> Transaction:
    return Transaction(
        posted_date=date(2026, 1, day),
        description=description,
        amount=Decimal(amount),
        source_page=1,
        source_line=source_line,
    )


def _stmt(
    transactions: list[Transaction],
    *,
    bank_name: str | None,
    printed_withdrawal_total: Decimal,
) -> ExtractedStatement:
    deposits = sum((t.amount for t in transactions if t.amount > 0), Decimal("0"))
    return ExtractedStatement(
        summary=StatementSummary(
            bank_name=bank_name,
            beginning_balance=Decimal("10000.00"),
            ending_balance=Decimal("10000.00")
            + deposits
            - sum((-t.amount for t in transactions if t.amount < 0), Decimal("0")),
            deposit_total=deposits,
            withdrawal_total=printed_withdrawal_total,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 28),
        ),
        transactions=transactions,
    )


def test_shadow_fires_when_td_drift_equals_service_charge() -> None:
    """LOAD LIFT TD pattern: $15 drift, one $15 MSF row → coercion would clear."""
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT PAYROLL", source_line=1),
        _txn(10, "-2000.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name="TD Bank N.A.", printed_withdrawal_total=Decimal("2000.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))

    # Routing unchanged: standard reconciliation still fails.
    assert result.passed is False
    assert any("reconciliation_failed_withdrawal_total" in f for f in result.failures), (
        result.failures
    )

    # Shadow warning records what coercion would do.
    matching = [
        w for w in result.warnings if w.startswith("shadow_td_withdrawal_coercion_would_clear:")
    ]
    assert len(matching) == 1, result.warnings
    assert "listed_2015.00" in matching[0]
    assert "printed_2000.00" in matching[0]
    assert "service_charges_15.00" in matching[0]
    assert "residual_0.00" in matching[0]


def test_shadow_marks_unattributed_when_drift_not_explained() -> None:
    """TD drift with no matching service-charge row → unattributed shadow flag."""
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT", source_line=1),
        _txn(10, "-2050.00", description="ELECTRONIC PMT VENDOR", source_line=2),
    ]
    stmt = _stmt(
        txns,
        bank_name="TD Bank Convenience Checking",
        printed_withdrawal_total=Decimal("2000.00"),
    )

    result = validate_extraction(stmt, today=date(2026, 2, 1))

    assert result.passed is False
    matching = [
        w for w in result.warnings if w.startswith("shadow_td_withdrawal_drift_unattributed:")
    ]
    assert len(matching) == 1, result.warnings
    assert "service_charges_0" in matching[0]


def test_shadow_silent_on_non_td_bank() -> None:
    """Same shape as TD case but bank_name='Chase' → no shadow warning."""
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT", source_line=1),
        _txn(10, "-2000.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name="JPMorgan Chase Bank", printed_withdrawal_total=Decimal("2000.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))

    assert not any(w.startswith("shadow_td_withdrawal_") for w in result.warnings), result.warnings


def test_shadow_silent_when_td_withdrawal_total_ties_out() -> None:
    """TD bank, no drift → no shadow flag at all."""
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT", source_line=1),
        _txn(10, "-2000.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name="TD Bank N.A.", printed_withdrawal_total=Decimal("2015.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))

    assert result.passed is True, (result.failures, result.warnings)
    assert not any(w.startswith("shadow_td_withdrawal_") for w in result.warnings), result.warnings


def test_shadow_silent_when_bank_name_missing() -> None:
    """No bank_name → can't classify as TD → no shadow flag."""
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT", source_line=1),
        _txn(10, "-2000.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name=None, printed_withdrawal_total=Decimal("2000.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))

    assert not any(w.startswith("shadow_td_withdrawal_") for w in result.warnings), result.warnings


def test_shadow_does_not_change_routing_when_coercion_would_clear() -> None:
    """The core safety property: shadow mode NEVER flips manual_review → proceed.

    LOAD LIFT's TD docs landed in manual_review and must stay there until
    operator validates the shadow audit window and flips the live coercion
    via config. This test pins that contract explicitly.
    """
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT", source_line=1),
        _txn(10, "-2000.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name="TD Bank N.A.", printed_withdrawal_total=Decimal("2000.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))

    assert result.passed is False
    assert any("reconciliation_failed_withdrawal_total" in f for f in result.failures)


def test_shadow_silent_when_printed_exceeds_listed() -> None:
    """Reverse drift direction (printed > listed) is a different bug class.

    Coercion only addresses the TD summary-split case where the printed
    total UNDER-counts withdrawals. If Bedrock dropped withdrawal rows so
    the printed total exceeds the sum, that's a separate problem and the
    coercion rule wouldn't fix it — so no shadow flag here.
    """
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT", source_line=1),
        _txn(10, "-1985.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name="TD Bank N.A.", printed_withdrawal_total=Decimal("2050.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))

    assert not any(w.startswith("shadow_td_withdrawal_") for w in result.warnings), result.warnings


# ---------------------------------------------------------------------------
# td_service_charge_total — unit
# ---------------------------------------------------------------------------


def test_td_service_charge_total_matches_msf_and_maintenance_variants() -> None:
    txns = [
        _txn(1, "-15.00", description="SERVICE CHARGE", source_line=1),
        _txn(2, "-25.00", description="MONTHLY MAINTENANCE FEE", source_line=2),
        _txn(3, "-12.00", description="Maintenance Fee — Business Acct", source_line=3),
        _txn(4, "-100.00", description="ACH PAYMENT VENDOR", source_line=4),  # excluded
    ]
    assert td_service_charge_total(txns) == Decimal("52.00")


def test_td_service_charge_total_excludes_positive_amount_rows() -> None:
    """A positive-amount row mentioning 'maintenance fee' (e.g. a refund) is
    not part of the withdrawal total we're trying to reconcile against."""
    txns = [
        _txn(1, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=1),
        _txn(2, "15.00", description="MAINTENANCE FEE REFUND", source_line=2),
    ]
    assert td_service_charge_total(txns) == Decimal("15.00")


def test_td_service_charge_total_empty_when_no_matches() -> None:
    txns = [
        _txn(1, "-100.00", description="ACH PAYMENT", source_line=1),
        _txn(2, "-50.00", description="WIRE OUT", source_line=2),
    ]
    assert td_service_charge_total(txns) == Decimal("0")
