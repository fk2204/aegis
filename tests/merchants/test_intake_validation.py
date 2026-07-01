"""Unit tests for ``aegis.merchants.intake_validation``.

Real cases from the 2026-07-01 audit are the primary fixtures:

* Fullerworks — rev=$55, req=$150,000 → suspicious revenue.
* Noble Horse — rev=$25,000, req=$1,350,000 → 54x request ratio.
* Turnbull — rev=$107,000, req=$4,000,000 → 37x request ratio.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.merchants.intake_validation import (
    IntakeWarning,
    validate_intake_financial_data,
)


def _codes(warnings: list[IntakeWarning]) -> set[str]:
    return {w.code for w in warnings}


def test_no_warnings_when_data_reasonable() -> None:
    """Trinity Envision — rev=$75K, req=$145K, ratio 1.9x. Should
    be clean."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("75000"),
        requested_amount=Decimal("145000"),
        stated_mca_balance=Decimal("125000"),
    )
    assert out == []


# ---------------------------------------------------------------------
# Rule 1 — suspicious revenue (Fullerworks case)
# ---------------------------------------------------------------------


def test_fullerworks_data_entry_error_fires_suspicious_revenue() -> None:
    """rev=$55 with req=$150,000 — the exact Fullerworks case."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("55"),
        requested_amount=Decimal("150000"),
    )
    assert "intake_revenue_suspiciously_low" in _codes(out)
    # The 10x rule ALSO fires ($150K / $55 = 2727x); both are correct.
    assert "intake_request_over_10x_revenue" in _codes(out)


def test_no_suspicious_revenue_when_request_small() -> None:
    """rev=$500 with req=$5,000 — request under the $10K floor, so
    Rule 1 does not fire even though revenue is < $1K."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("500"),
        requested_amount=Decimal("5000"),
    )
    assert "intake_revenue_suspiciously_low" not in _codes(out)


def test_no_suspicious_revenue_at_the_threshold() -> None:
    """rev exactly at $1000 does NOT trip Rule 1 (< $1000, strict)."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("1000"),
        requested_amount=Decimal("50000"),
    )
    assert "intake_revenue_suspiciously_low" not in _codes(out)


# ---------------------------------------------------------------------
# Rule 2 — impossible request ratio (Noble Horse + Turnbull)
# ---------------------------------------------------------------------


def test_noble_horse_54x_ratio_fires() -> None:
    """rev=$25K, req=$1.35M — 54x. The exact Noble Horse case."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("25000"),
        requested_amount=Decimal("1350000"),
    )
    assert "intake_request_over_10x_revenue" in _codes(out)


def test_turnbull_37x_ratio_fires() -> None:
    """rev=$107K, req=$4M — 37x. The Turnbull case."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("107000"),
        requested_amount=Decimal("4000000"),
    )
    assert "intake_request_over_10x_revenue" in _codes(out)


def test_ratio_at_threshold_does_not_fire() -> None:
    """req = 10x rev exactly does NOT fire (> is strict)."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("10000"),
        requested_amount=Decimal("100000"),
    )
    assert "intake_request_over_10x_revenue" not in _codes(out)


def test_normal_mca_ratio_does_not_fire() -> None:
    """1.2x is a normal MCA advance size."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("50000"),
        requested_amount=Decimal("60000"),
    )
    assert "intake_request_over_10x_revenue" not in _codes(out)


# ---------------------------------------------------------------------
# Rule 3 — impossible existing MCA balance
# ---------------------------------------------------------------------


def test_showcase_balance_ratio_fires() -> None:
    """rev=$20K, balance=$60K — 3.0x. Just past the 3x threshold."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("20000"),
        requested_amount=Decimal("40000"),
        stated_mca_balance=Decimal("60001"),
    )
    assert "intake_balance_over_3x_revenue" in _codes(out)


def test_balance_at_threshold_does_not_fire() -> None:
    """balance = 3x rev exactly does NOT fire (> is strict)."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("20000"),
        requested_amount=Decimal("40000"),
        stated_mca_balance=Decimal("60000"),
    )
    assert "intake_balance_over_3x_revenue" not in _codes(out)


# ---------------------------------------------------------------------
# Missing / weird inputs — degrade to no warnings, never raise
# ---------------------------------------------------------------------


def test_none_inputs_no_warnings() -> None:
    out = validate_intake_financial_data(
        monthly_revenue=None,
        requested_amount=None,
        stated_mca_balance=None,
    )
    assert out == []


def test_zero_revenue_no_warnings() -> None:
    """Zero-revenue rows skip both revenue-based rules cleanly."""
    out = validate_intake_financial_data(
        monthly_revenue=Decimal("0"),
        requested_amount=Decimal("50000"),
    )
    assert out == []


def test_int_and_float_inputs_accepted() -> None:
    """The webhook layer often has raw ints/floats; coerce cleanly."""
    out = validate_intake_financial_data(
        monthly_revenue=25000,
        requested_amount=1350000.0,
    )
    assert "intake_request_over_10x_revenue" in _codes(out)


def test_bool_input_treated_as_missing() -> None:
    """``bool`` is an int subclass but no revenue field should ever
    contain True/False; treat as None rather than 1/0."""
    out = validate_intake_financial_data(
        monthly_revenue=True,
        requested_amount=50000,
    )
    assert out == []
