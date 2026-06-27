"""Tests for ``parse_product_type_safe`` (migration 080).

The Close ``Product Type`` choice field maps to AEGIS's ProductType
literal via case-insensitive substring match. Unknown values surface a
warning token so the webhook handler can audit-log the drift.
"""

from __future__ import annotations

import pytest

from aegis.close.field_map import parse_product_type_safe


@pytest.mark.parametrize(
    ("close_value", "expected_literal"),
    [
        # Revenue-based (default + multiple aliases)
        ("Revenue Based", "revenue_based"),
        ("Revenue-Based", "revenue_based"),
        ("MCA", "revenue_based"),
        ("Merchant Cash Advance", "revenue_based"),
        ("RBF", "revenue_based"),
        ("revenue based financing", "revenue_based"),  # case-insensitive
        # Business loan
        ("Term Loan", "business_loan"),
        ("Business Loan", "business_loan"),
        ("SBA Loan", "business_loan"),
        ("Installment Loan", "business_loan"),
        # Line of credit
        ("Line of Credit", "line_of_credit"),
        ("LOC", "line_of_credit"),
        ("Revolving Credit", "line_of_credit"),
        # Equipment
        ("Equipment Lease", "equipment"),
        ("Equipment Finance", "equipment"),
        ("Equipment Financing", "equipment"),
        ("Equipment Loan", "equipment"),
        # Asset-based
        ("Asset Based", "asset_based"),
        ("Asset-Based Lending", "asset_based"),
        ("ABL", "asset_based"),
        # Receivables / factoring
        ("Receivables", "receivables"),
        ("Factoring", "receivables"),
        ("Invoice Factoring", "receivables"),
        ("A/R Financing", "receivables"),
    ],
)
def test_known_close_values_map_to_correct_literal(close_value: str, expected_literal: str) -> None:
    literal, warning = parse_product_type_safe(close_value)
    assert literal == expected_literal
    assert warning is None


def test_unknown_value_returns_warning() -> None:
    """An unrecognized Close choice surfaces in the warning slot so
    the webhook caller writes a ``close.field_parse_warning`` audit row.
    """
    literal, warning = parse_product_type_safe("Garbage Product")
    assert literal is None
    assert warning == "Garbage Product"


def test_none_input_returns_double_none() -> None:
    """Null-shaped input collapses to no value + no warning."""
    literal, warning = parse_product_type_safe(None)
    assert literal is None
    assert warning is None


def test_empty_string_collapses_to_none() -> None:
    """Empty string after strip → no value + no warning (treated as null)."""
    literal, warning = parse_product_type_safe("")
    assert literal is None
    assert warning is None


def test_close_none_marker_collapses_to_none() -> None:
    """Close's ``-None-`` sentinel is treated as null per the same
    convention as the other ``_safe`` parsers in this module.
    """
    literal, warning = parse_product_type_safe("-None-")
    assert literal is None
    assert warning is None


def test_non_string_input_returns_warning() -> None:
    """A non-string surprise (e.g. Close sends a number) should not
    raise — it returns a warning so the operator sees the drift.
    """
    literal, warning = parse_product_type_safe(42)
    assert literal is None
    assert warning == "42"


def test_substring_match_is_case_insensitive() -> None:
    """Operators may type the Close choice in any case."""
    for value in ("TERM LOAN", "term loan", "Term Loan", "tErM lOaN"):
        literal, warning = parse_product_type_safe(value)
        assert literal == "business_loan"
        assert warning is None


def test_whitespace_preserved_in_warning() -> None:
    """When an unrecognized value surfaces in the warning slot, the
    stripped form lands so audit-log filtering can normalize on it.
    """
    literal, warning = parse_product_type_safe("  Unknown Product  ")
    assert literal is None
    assert warning == "Unknown Product"
