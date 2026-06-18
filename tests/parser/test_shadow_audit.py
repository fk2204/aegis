"""Tests for ``aegis.parser.shadow_audit``.

Two layers:

* Pure-function string parsing — fast, covers the format the worker
  consumes.
* Round-trip with ``validate_extraction`` — runs the real shadow check,
  feeds its warnings into ``shadow_audit_payloads``, asserts the parsing
  survives. Catches format drift between ``validate.py`` and this module
  (the failure mode the pure-function tests can't see on their own).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aegis.parser.models import (
    ExtractedStatement,
    StatementSummary,
    Transaction,
)
from aegis.parser.shadow_audit import shadow_audit_payloads
from aegis.parser.validate import validate_extraction

# ---------------------------------------------------------------------------
# Pure-function parsing
# ---------------------------------------------------------------------------


def test_would_clear_warning_translates_to_one_audit_payload() -> None:
    warnings = [
        "shadow_td_withdrawal_coercion_would_clear:"
        "listed_2015.00_printed_2000.00_service_charges_15.00_residual_0.00"
    ]
    payloads = shadow_audit_payloads(warnings)
    assert len(payloads) == 1
    p = payloads[0]
    assert p.action == "parser.shadow.td_withdrawal_coercion"
    assert p.details["outcome"] == "coercion_would_clear"
    assert p.details["listed"] == "2015.00"
    assert p.details["printed"] == "2000.00"
    assert p.details["service_charges"] == "15.00"
    assert p.details["residual"] == "0.00"
    assert p.details["raw"] == warnings[0]


def test_drift_unattributed_warning_translates() -> None:
    warnings = [
        "shadow_td_withdrawal_drift_unattributed:"
        "listed_2050.00_printed_2000.00_service_charges_0_residual_50.00"
    ]
    payloads = shadow_audit_payloads(warnings)
    assert len(payloads) == 1
    assert payloads[0].details["outcome"] == "drift_unattributed"
    assert payloads[0].details["residual"] == "50.00"


def test_negative_residual_round_trips() -> None:
    """Residual is signed — coercion that *over*-shoots prints negative."""
    warnings = [
        "shadow_td_withdrawal_coercion_would_clear:"
        "listed_2010.00_printed_2000.00_service_charges_15.00_residual_-5.00"
    ]
    payloads = shadow_audit_payloads(warnings)
    assert len(payloads) == 1
    assert payloads[0].details["residual"] == "-5.00"


def test_unmatched_warning_is_dropped_silently() -> None:
    """Non-TD shadow flags (daily-balance break, txn-id gap) pass through."""
    warnings = [
        "daily_balance_continuity_break:2026-01-15_expected_5000.00_actual_5001.50_diff_1.50",
        "transaction_id_sequence_gap:1234_1240_5",
        "shadow_td_withdrawal_drift_unattributed:"
        "listed_2050.00_printed_2000.00_service_charges_0_residual_50.00",
    ]
    payloads = shadow_audit_payloads(warnings)
    assert len(payloads) == 1
    assert payloads[0].details["outcome"] == "drift_unattributed"


def test_empty_warning_list_returns_empty() -> None:
    assert shadow_audit_payloads([]) == []


def test_malformed_warning_is_dropped_silently() -> None:
    """A close-but-not-quite string doesn't match and doesn't crash."""
    warnings = [
        "shadow_td_withdrawal_coercion_would_clear",  # no payload
        "shadow_td_withdrawal_coercion_would_clear:listed_x_printed_y",  # missing fields
        "shadow_td_withdrawal_other_outcome:listed_1_printed_1_service_charges_0_residual_0",
    ]
    assert shadow_audit_payloads(warnings) == []


def test_multiple_warnings_yield_multiple_payloads() -> None:
    """Order-preserving 1:1 mapping."""
    warnings = [
        "shadow_td_withdrawal_coercion_would_clear:"
        "listed_2015.00_printed_2000.00_service_charges_15.00_residual_0.00",
        "shadow_td_withdrawal_drift_unattributed:"
        "listed_2050.00_printed_2000.00_service_charges_0_residual_50.00",
    ]
    payloads = shadow_audit_payloads(warnings)
    assert [p.details["outcome"] for p in payloads] == [
        "coercion_would_clear",
        "drift_unattributed",
    ]


# ---------------------------------------------------------------------------
# Round-trip with the real validate gate
# ---------------------------------------------------------------------------


def _txn(day: int, amount: str, *, description: str = "TXN", source_line: int = 1) -> Transaction:
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


def test_round_trip_td_would_clear_warning_through_validate() -> None:
    """The format validate.py emits must parse back. Catches format drift."""
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT PAYROLL", source_line=1),
        _txn(10, "-2000.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name="TD Bank N.A.", printed_withdrawal_total=Decimal("2000.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))
    payloads = shadow_audit_payloads(list(result.warnings))

    assert len(payloads) == 1, result.warnings
    p = payloads[0]
    assert p.action == "parser.shadow.td_withdrawal_coercion"
    assert p.details["outcome"] == "coercion_would_clear"
    assert p.details["listed"] == "2015.00"
    assert p.details["printed"] == "2000.00"
    assert p.details["service_charges"] == "15.00"
    assert p.details["residual"] == "0.00"


def test_round_trip_td_drift_unattributed_warning_through_validate() -> None:
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
    payloads = shadow_audit_payloads(list(result.warnings))

    assert len(payloads) == 1, result.warnings
    assert payloads[0].details["outcome"] == "drift_unattributed"
    assert payloads[0].details["residual"] == "50.00"


def test_round_trip_non_td_bank_yields_no_payloads() -> None:
    """Non-TD banks don't get a shadow flag and therefore no audit row."""
    txns = [
        _txn(2, "5000.00", description="ACH CREDIT", source_line=1),
        _txn(10, "-2000.00", description="ELECTRONIC PMT VENDOR", source_line=2),
        _txn(15, "-15.00", description="MONTHLY MAINTENANCE FEE", source_line=3),
    ]
    stmt = _stmt(txns, bank_name="JPMorgan Chase Bank", printed_withdrawal_total=Decimal("2000.00"))

    result = validate_extraction(stmt, today=date(2026, 2, 1))
    assert shadow_audit_payloads(list(result.warnings)) == []
