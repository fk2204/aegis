"""Tests for ``_parse_close_lead_description`` (migration 087).

Verifies the FINANCIAL-block parser correctly extracts merchant-typed
application-intake data from the Close Lead ``description`` text field.

The parser's contract:
  * Returns ``{}`` when the description is missing / empty / has no
    FINANCIAL header.
  * Returns a dict keyed by ``MerchantRow`` field names.
  * Money fields → ``Decimal | None``; int fields → ``int | None``;
    list fields → ``list[str]`` (empty list when blank); free-text →
    ``str | None``.
  * NEVER raises — bad money / int strings collapse to ``None`` so the
    webhook caller doesn't lose every other field that did parse.
"""

from __future__ import annotations

from decimal import Decimal

from aegis.close.field_map import _parse_close_lead_description

# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


_HAPPY_PATH_DESCRIPTION = """\
Inbound from broker — funded once before, wants more.

FINANCIAL:
  Requested Amount: 499999
  Use of Funds: Expansion / Build-out
  Monthly Gross Revenue: 199999
  Avg Monthly CC Sales: 200000
  Monthly Deposits: 20
  Existing MCA Positions: 2
  Current Lenders: Diesel, Finpoint
  Existing MCA Balance: 8001
  Daily/Weekly Payment: 125000
  Bank: Third Coast Bank

OWNER:
  Name: Jane Doe
  Phone: 555-0100
"""


def test_happy_path_extracts_every_field() -> None:
    out = _parse_close_lead_description(_HAPPY_PATH_DESCRIPTION)

    assert out["requested_amount"] == Decimal("499999")
    assert out["use_of_funds"] == "Expansion / Build-out"
    assert out["monthly_revenue"] == Decimal("199999")
    assert out["avg_monthly_cc_sales"] == Decimal("200000")
    assert out["stated_monthly_deposits"] == 20
    assert out["stated_mca_positions"] == 2
    assert out["stated_current_lenders"] == ["Diesel", "Finpoint"]
    assert out["stated_mca_balance"] == Decimal("8001")
    assert out["stated_daily_payment"] == Decimal("125000")
    assert out["stated_bank"] == "Third Coast Bank"


def test_owner_block_does_not_bleed_into_financial() -> None:
    """The OWNER section that follows the FINANCIAL block must not
    leak its KV lines back into the financial dict."""
    out = _parse_close_lead_description(_HAPPY_PATH_DESCRIPTION)
    # OWNER's keys ("Name", "Phone") are not in the label table; the
    # parser stops at the blank line separating the blocks so neither
    # appears in the output.
    assert "Name" not in out
    assert "Phone" not in out


# ---------------------------------------------------------------------
# Missing fields
# ---------------------------------------------------------------------


def test_only_some_fields_present() -> None:
    desc = """\
FINANCIAL:
  Requested Amount: 100000
  Bank: Chase
"""
    out = _parse_close_lead_description(desc)
    assert out == {
        "requested_amount": Decimal("100000"),
        "stated_bank": "Chase",
    }


def test_blank_value_collapses_to_none_for_money_field() -> None:
    desc = """\
FINANCIAL:
  Monthly Gross Revenue:
  Bank: Chase
"""
    out = _parse_close_lead_description(desc)
    assert out["monthly_revenue"] is None
    assert out["stated_bank"] == "Chase"


def test_blank_lenders_collapses_to_empty_list() -> None:
    desc = """\
FINANCIAL:
  Current Lenders:
"""
    out = _parse_close_lead_description(desc)
    # Empty list, NOT None — matches the DB DEFAULT (ARRAY[]).
    assert out["stated_current_lenders"] == []


# ---------------------------------------------------------------------
# Malformed values
# ---------------------------------------------------------------------


def test_malformed_money_collapses_to_none() -> None:
    desc = """\
FINANCIAL:
  Monthly Gross Revenue: not a number
  Bank: Chase
"""
    out = _parse_close_lead_description(desc)
    assert out["monthly_revenue"] is None
    # Other fields still parse — the bad line is isolated.
    assert out["stated_bank"] == "Chase"


def test_money_with_dollar_sign_and_commas() -> None:
    desc = """\
FINANCIAL:
  Requested Amount: $1,234.56
  Existing MCA Balance: $8,001
"""
    out = _parse_close_lead_description(desc)
    assert out["requested_amount"] == Decimal("1234.56")
    assert out["stated_mca_balance"] == Decimal("8001")


def test_malformed_int_collapses_to_none() -> None:
    desc = """\
FINANCIAL:
  Existing MCA Positions: many
"""
    out = _parse_close_lead_description(desc)
    assert out["stated_mca_positions"] is None


def test_int_with_dollar_sign_or_comma_strips_cleanly() -> None:
    desc = """\
FINANCIAL:
  Monthly Deposits: 1,200
"""
    out = _parse_close_lead_description(desc)
    assert out["stated_monthly_deposits"] == 1200


# ---------------------------------------------------------------------
# Edge inputs
# ---------------------------------------------------------------------


def test_none_description_returns_empty_dict() -> None:
    assert _parse_close_lead_description(None) == {}


def test_empty_string_returns_empty_dict() -> None:
    assert _parse_close_lead_description("") == {}


def test_whitespace_only_returns_empty_dict() -> None:
    assert _parse_close_lead_description("   \n  \n") == {}


def test_description_without_financial_block_returns_empty_dict() -> None:
    desc = "Just a plain text description with no FINANCIAL block here."
    assert _parse_close_lead_description(desc) == {}


def test_edge_whitespace_around_keys_and_values() -> None:
    """Operator drift on indentation + trailing whitespace must not
    break the parse."""
    desc = "FINANCIAL:\n   Requested Amount:   500000   \n\tBank:\tWells Fargo  \n"
    out = _parse_close_lead_description(desc)
    assert out["requested_amount"] == Decimal("500000")
    assert out["stated_bank"] == "Wells Fargo"


def test_label_case_insensitive() -> None:
    """Operator drift on label capitalization must absorb gracefully."""
    desc = """\
FINANCIAL:
  REQUESTED AMOUNT: 100
  monthly gross revenue: 200
"""
    out = _parse_close_lead_description(desc)
    assert out["requested_amount"] == Decimal("100")
    assert out["monthly_revenue"] == Decimal("200")


def test_current_lenders_trims_each_entry() -> None:
    desc = """\
FINANCIAL:
  Current Lenders:  Diesel ,  Finpoint  , Velocity
"""
    out = _parse_close_lead_description(desc)
    assert out["stated_current_lenders"] == ["Diesel", "Finpoint", "Velocity"]


def test_current_lenders_drops_empty_segments() -> None:
    """Trailing commas / double commas must not produce empty strings."""
    desc = """\
FINANCIAL:
  Current Lenders: Diesel,,Finpoint,
"""
    out = _parse_close_lead_description(desc)
    assert out["stated_current_lenders"] == ["Diesel", "Finpoint"]


def test_unrecognized_line_with_value_skipped_not_stopped() -> None:
    """A label we don't map but with a value (operator added a new
    line we haven't wired) is skipped silently — subsequent valid
    lines still parse."""
    desc = """\
FINANCIAL:
  Some New Field We Don't Know: yes
  Requested Amount: 1000
"""
    out = _parse_close_lead_description(desc)
    assert out["requested_amount"] == Decimal("1000")
