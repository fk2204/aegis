"""Unit tests for ``_coerce_summary`` — Brex-style edge cases.

Regression: Brex statements render the period's beginning balance as a
dash when the account starts at $0 / fresh. Claude correctly extracts
that as ``null``, but the older code path passed None through to the
Pydantic ``Money`` field which rejects it, blocking the whole parse.
"""

from __future__ import annotations

from aegis.parser.extract import _coerce_summary


def test_coerce_summary_null_beginning_balance_becomes_zero() -> None:
    """Brex's '$-' beginning balance must coerce to $0.00, not raise."""
    payload = {
        "beginning_balance": None,
        "ending_balance": 24143.69,
        "deposit_total": 135240.02,
        "withdrawal_total": 111096.33,
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
    }
    out = _coerce_summary(payload)
    assert out["beginning_balance"] == "0.00"
    # Other coerced fields kept as strings.
    assert out["ending_balance"] == "24143.69"
    assert out["deposit_total"] == "135240.02"
    # withdrawal_total takes absolute value of the printed positive total.
    assert out["withdrawal_total"] == "111096.33"


def test_coerce_summary_preserves_real_beginning_balance() -> None:
    payload = {
        "beginning_balance": 288.29,
        "ending_balance": 24143.69,
        "deposit_total": 135240.02,
        "withdrawal_total": 111096.33,
    }
    out = _coerce_summary(payload)
    assert out["beginning_balance"] == "288.29"


def test_coerce_summary_null_ending_balance_becomes_zero() -> None:
    """Same Brex-style edge case applies to ending_balance — coerce null → 0."""
    payload = {
        "beginning_balance": 288.29,
        "ending_balance": None,
        "deposit_total": 12345.67,
        "withdrawal_total": 9876.54,
    }
    out = _coerce_summary(payload)
    assert out["ending_balance"] == "0.00"
    assert out["beginning_balance"] == "288.29"


def test_coerce_summary_both_balances_null() -> None:
    """Both ends null → both zero; downstream validation gate flags the
    mismatch with the transaction stream as manual_review (not a hard
    parse failure)."""
    payload = {
        "beginning_balance": None,
        "ending_balance": None,
        "deposit_total": 100.00,
        "withdrawal_total": 50.00,
    }
    out = _coerce_summary(payload)
    assert out["beginning_balance"] == "0.00"
    assert out["ending_balance"] == "0.00"
