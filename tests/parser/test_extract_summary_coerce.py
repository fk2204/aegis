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
