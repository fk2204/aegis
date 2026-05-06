"""Tests for aegis.money — Decimal helpers and float rejection."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aegis.money import (
    DEFAULT_TOL,
    FloatMoneyError,
    as_money,
    money_eq,
    safe_divide,
    to_cents,
)


class TestAsMoney:
    def test_accepts_string(self) -> None:
        assert as_money("12.34") == Decimal("12.34")

    def test_accepts_decimal(self) -> None:
        assert as_money(Decimal("12.34")) == Decimal("12.34")

    def test_accepts_int(self) -> None:
        assert as_money(100) == Decimal("100.00")

    def test_rejects_float(self) -> None:
        with pytest.raises(FloatMoneyError):
            as_money(12.34)  # type: ignore[arg-type]

    def test_rounds_half_up(self) -> None:
        # Banker's rounding would give 0.12; we want 0.13.
        assert as_money("0.125") == Decimal("0.13")

    def test_quantizes_to_two_dp(self) -> None:
        assert as_money("12.3456") == Decimal("12.35")

    def test_rejects_bytes(self) -> None:
        with pytest.raises(TypeError):
            as_money(b"12.34")  # type: ignore[arg-type]


class TestToCents:
    def test_dollars_to_cents(self) -> None:
        assert to_cents(Decimal("12.34")) == 1234

    def test_zero(self) -> None:
        assert to_cents(Decimal("0")) == 0

    def test_rounding(self) -> None:
        # 12.345 rounds half-up to 1235 cents
        assert to_cents(Decimal("12.345")) == 1235


class TestMoneyEq:
    def test_within_default_tolerance(self) -> None:
        assert money_eq(Decimal("100.00"), Decimal("100.01"))

    def test_outside_default_tolerance(self) -> None:
        assert not money_eq(Decimal("100.00"), Decimal("100.02"))

    def test_explicit_tolerance(self) -> None:
        assert money_eq(Decimal("100.00"), Decimal("100.50"), tol=Decimal("1.00"))

    def test_default_tol_is_one_cent(self) -> None:
        assert DEFAULT_TOL == Decimal("0.01")


class TestSafeDivide:
    def test_normal_division(self) -> None:
        assert safe_divide(Decimal("100"), Decimal("4")) == Decimal("25.00")

    def test_zero_denominator_returns_zero(self) -> None:
        assert safe_divide(Decimal("100"), Decimal("0")) == Decimal("0")

    def test_zero_over_zero_returns_zero(self) -> None:
        assert safe_divide(Decimal("0"), Decimal("0")) == Decimal("0")

    def test_quantizes_result(self) -> None:
        assert safe_divide(Decimal("10"), Decimal("3")) == Decimal("3.33")
