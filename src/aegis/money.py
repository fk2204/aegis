"""Decimal-based money helpers.

Money is `Decimal`. `float` is forbidden — `as_money(float)` raises so the
operator is forced to convert at the boundary (parse JSON to str, then to
Decimal). This is the only safe way to keep money math correct.

Set `getcontext().prec = 28` here so any module that imports `aegis.money`
inherits the precision — sufficient for 14-digit money totals (`numeric(14,2)`)
plus rate calculations.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, getcontext
from typing import Annotated

from pydantic import Field

# Set once at import time. Imports of aegis.money are early on the boot path
# (config -> models -> money), so this runs before any aggregate math.
getcontext().prec = 28

# Standard cent-rounding quantizer. Bank statements always quote 2dp.
_CENT = Decimal("0.01")

# Default tolerance for tie-out checks (parser validation, etc.). $0.01.
DEFAULT_TOL = _CENT

# Pydantic field annotation for any money column.
# numeric(14,2) on the DB side -> max 12 integer digits + 2 decimals.
Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]


class FloatMoneyError(TypeError):
    """Raised when a float is passed where money is required."""


def as_money(value: str | int | Decimal) -> Decimal:
    """Coerce a value to a 2dp Decimal. Floats are rejected.

    Floats can't represent 0.10 exactly, so accepting them silently invites
    rounding errors. Convert at the JSON / DB boundary instead — pass the
    string form into here.
    """
    if isinstance(value, float):
        raise FloatMoneyError(
            "as_money refuses float input — convert to str first to preserve precision"
        )
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        d = Decimal(value)
    else:
        raise TypeError(f"as_money cannot accept {type(value).__name__}")
    return d.quantize(_CENT, rounding=ROUND_HALF_UP)


def to_cents(value: Decimal) -> int:
    """Decimal dollars -> integer cents. Useful for hashing / id math."""
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money_eq(a: Decimal, b: Decimal, tol: Decimal = DEFAULT_TOL) -> bool:
    """Compare two money values with explicit tolerance. Never use `==`."""
    return abs(a - b) <= tol


def safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Division that returns 0 on zero denominator instead of raising.

    Aggregates often divide by counts that may be zero (e.g.
    avg_daily_balance over a 0-day window). Returning 0 is the operator's
    intent — the surrounding aggregate already records `_source_ids`, so
    the empty case is auditable without a divide-by-zero crash.
    """
    if denominator == 0:
        return Decimal("0")
    return (numerator / denominator).quantize(_CENT, rounding=ROUND_HALF_UP)
