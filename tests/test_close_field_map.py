"""Pure-function tests for close/field_map.py.

Coverage per the operator's spec:

* FICO Range: every defined band + below-floor + above-ceiling + None
  + garbage string (must raise).
* Industry → NAICS: every entry in the static table + an unknown
  industry (must raise, not silently default).
* Entity type dedup: both null, only A, only B, both agree, both
  disagree (audit row asserted on conflict; conflict without audit
  is still safe).
* Money: every edge case from the spec, with explicit assertions.

No HTTP, no DB, no settings reads.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.field_map import (
    CLOSE_ENTITY_TYPE_TO_AEGIS,
    CLOSE_FIELD_IDS,
    CLOSE_INDUSTRY_TO_NAICS,
    FICO_RANGE_LOWER_BOUND,
    FieldMapError,
    filename_is_non_statement,
    filename_matches_statement_filter,
    get_custom_field,
    industry_to_naics,
    normalize_entity_type,
    parse_fico_range,
    parse_money,
    resolve_entity_type,
)

# ----------------------------------------------------------------------
# FICO Range
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<550", 549),
        ("550-599", 550),
        ("600-649", 600),
        ("650-699", 650),
        ("700+", 700),
    ],
)
def test_parse_fico_range_every_defined_band(value: str, expected: int) -> None:
    """Each band in FICO_RANGE_LOWER_BOUND maps to its lower-bound int."""
    assert parse_fico_range(value) == expected


def test_parse_fico_range_covers_every_table_entry() -> None:
    """The parametrized list above must cover the whole table — if a
    new band lands in FICO_RANGE_LOWER_BOUND without a parametrize
    entry, this test fails."""
    parametrized = {"<550", "550-599", "600-649", "650-699", "700+"}
    assert set(FICO_RANGE_LOWER_BOUND.keys()) == parametrized, (
        "FICO_RANGE_LOWER_BOUND drifted; add the new band to the parametrize list above."
    )


def test_parse_fico_range_none_returns_none() -> None:
    assert parse_fico_range(None) is None


def test_parse_fico_range_empty_string_returns_none() -> None:
    assert parse_fico_range("") is None


def test_parse_fico_range_none_marker_returns_none() -> None:
    assert parse_fico_range("-None-") is None


def test_parse_fico_range_below_floor_raises() -> None:
    """Values below the documented <550 floor (e.g. "<450" — not in the
    table) must raise rather than silently defaulting."""
    with pytest.raises(FieldMapError, match="unknown FICO Range value"):
        parse_fico_range("<450")


def test_parse_fico_range_above_ceiling_raises() -> None:
    """Values above the documented 700+ ceiling (e.g. "800+") must
    raise rather than silently defaulting."""
    with pytest.raises(FieldMapError, match="unknown FICO Range value"):
        parse_fico_range("800+")


def test_parse_fico_range_garbage_string_raises() -> None:
    with pytest.raises(FieldMapError, match="unknown FICO Range value"):
        parse_fico_range("not a fico bucket")


# ----------------------------------------------------------------------
# Industry → NAICS
# ----------------------------------------------------------------------


@pytest.mark.parametrize("industry", sorted(CLOSE_INDUSTRY_TO_NAICS.keys()))
def test_industry_to_naics_every_table_entry(industry: str) -> None:
    """Every Industry in the static table maps to a 6-digit code that
    matches the table value."""
    code = industry_to_naics(industry)
    assert code == CLOSE_INDUSTRY_TO_NAICS[industry]
    assert code is not None and len(code) == 6 and code.isdigit()


def test_industry_to_naics_table_has_18_entries() -> None:
    """The Close Industry choice list (verified 2026-05-20) has 18
    entries. If Close adds or removes a choice, this test fails so
    the table can be reviewed."""
    assert len(CLOSE_INDUSTRY_TO_NAICS) == 18


def test_industry_to_naics_unknown_industry_raises() -> None:
    with pytest.raises(FieldMapError, match="unknown Close Industry"):
        industry_to_naics("Cryptocurrency Mining")


def test_industry_to_naics_none_returns_none() -> None:
    assert industry_to_naics(None) is None


def test_industry_to_naics_empty_returns_none() -> None:
    assert industry_to_naics("") is None


def test_industry_to_naics_none_marker_returns_none() -> None:
    assert industry_to_naics("-None-") is None


# ----------------------------------------------------------------------
# Entity type dedup
# ----------------------------------------------------------------------


def test_resolve_entity_type_both_null_returns_none() -> None:
    audit = InMemoryAuditLog()
    result = resolve_entity_type(
        entity_type_a=None,
        entity_type_b=None,
        close_lead_id="lead_abc",
        audit=audit,
    )
    assert result is None
    assert audit.entries == []


def test_resolve_entity_type_only_a_set() -> None:
    audit = InMemoryAuditLog()
    result = resolve_entity_type(
        entity_type_a="LLC",
        entity_type_b=None,
        close_lead_id="lead_abc",
        audit=audit,
    )
    assert result == "LLC"
    assert audit.entries == []


def test_resolve_entity_type_only_b_set() -> None:
    audit = InMemoryAuditLog()
    result = resolve_entity_type(
        entity_type_a=None,
        entity_type_b="S-Corp",
        close_lead_id="lead_abc",
        audit=audit,
    )
    assert result == "S-Corp"
    assert audit.entries == []


def test_resolve_entity_type_both_agree_returns_value() -> None:
    audit = InMemoryAuditLog()
    result = resolve_entity_type(
        entity_type_a="LLC",
        entity_type_b="LLC",
        close_lead_id="lead_abc",
        audit=audit,
    )
    assert result == "LLC"
    assert audit.entries == []


def test_resolve_entity_type_disagree_audits_and_prefers_a() -> None:
    """Disagreement -> one audit row, action close.field_map.entity_type_conflict,
    details carrying both values + close_lead_id. Resolution = entity_type_a."""
    audit = InMemoryAuditLog()
    result = resolve_entity_type(
        entity_type_a="LLC",
        entity_type_b="S-Corp",
        close_lead_id="lead_xyz",
        audit=audit,
    )
    assert result == "LLC"
    assert len(audit.entries) == 1
    row = audit.entries[0]
    assert row["actor"] == "close_field_map"
    assert row["action"] == "close.field_map.entity_type_conflict"
    assert row["details"] == {
        "close_lead_id": "lead_xyz",
        "entity_type_a": "LLC",
        "entity_type_b": "S-Corp",
        "resolved_to": "LLC",
    }


def test_resolve_entity_type_disagree_without_audit_does_not_raise() -> None:
    """Audit is optional. Conflict path must still succeed without one."""
    result = resolve_entity_type(
        entity_type_a="LLC",
        entity_type_b="S-Corp",
        close_lead_id="lead_xyz",
        audit=None,
    )
    assert result == "LLC"


def test_resolve_entity_type_treats_none_marker_as_null() -> None:
    """Close's "-None-" sentinel must be treated as null, not as a real
    Entity type value."""
    result = resolve_entity_type(
        entity_type_a="-None-",
        entity_type_b="LLC",
        close_lead_id="lead_abc",
    )
    assert result == "LLC"


def test_resolve_entity_type_audit_write_failure_does_not_propagate() -> None:
    """If the audit sink raises, the field-map call still returns a
    value (the conflict signal is non-blocking by design)."""

    class _ExplodingAudit:
        def record(self, **kwargs: object) -> None:
            raise RuntimeError("audit DB down")

    result = resolve_entity_type(
        entity_type_a="LLC",
        entity_type_b="S-Corp",
        close_lead_id="lead_xyz",
        audit=_ExplodingAudit(),  # type: ignore[arg-type]
    )
    assert result == "LLC"


# ----------------------------------------------------------------------
# Money parsing
# ----------------------------------------------------------------------


def test_parse_money_dollar_sign_and_commas() -> None:
    assert parse_money("$1,500.00") == Decimal("1500.00")


def test_parse_money_dollar_sign_and_commas_preserves_two_decimals() -> None:
    """Ensure the two-decimal precision is preserved, not stripped."""
    assert str(parse_money("$1,500.00")) == "1500.00"


def test_parse_money_no_formatting() -> None:
    assert parse_money("1500") == Decimal("1500")


def test_parse_money_one_decimal_preserves_precision() -> None:
    """The operator typed one decimal; we preserve that, NOT normalize
    to two decimals. parse_money is a parser, not a normalizer."""
    assert str(parse_money("$1,500.5")) == "1500.5"


def test_parse_money_empty_string_returns_none() -> None:
    assert parse_money("") is None


def test_parse_money_none_returns_none() -> None:
    assert parse_money(None) is None


def test_parse_money_garbage_raises() -> None:
    with pytest.raises(FieldMapError, match="could not parse money"):
        parse_money("not a number")


def test_parse_money_multiple_decimals_raises() -> None:
    with pytest.raises(FieldMapError, match="could not parse money"):
        parse_money("1.2.3")


def test_parse_money_only_dollar_sign_raises() -> None:
    """Strip leaves nothing -> raise (don't silently treat as None)."""
    with pytest.raises(FieldMapError, match="collapsed to empty"):
        parse_money("$")


def test_parse_money_only_commas_raises() -> None:
    with pytest.raises(FieldMapError, match="collapsed to empty"):
        parse_money(",,,")


def test_parse_money_never_uses_float() -> None:
    """Pass a float (testing belt-and-suspenders). The implementation
    coerces via str(), so the binary-float gotcha is avoided.
    Decimal("1.1") and Decimal(str(1.1)) both equal Decimal("1.1").
    """
    # 0.1 + 0.2 == 0.30000000000000004 in float. parse_money must NOT
    # surface that via Decimal(float) — it must stringify first.
    result = parse_money(1.1)
    assert result == Decimal("1.1"), f"expected Decimal('1.1') via str() coercion; got {result!r}"


def test_parse_money_strips_whitespace() -> None:
    assert parse_money("  $1,500.00  ") == Decimal("1500.00")


# ----------------------------------------------------------------------
# Custom-field accessor
# ----------------------------------------------------------------------


def test_get_custom_field_pulls_value_by_aegis_name() -> None:
    payload = {
        f"custom.{CLOSE_FIELD_IDS['fico_range']}": "650-699",
        "id": "lead_abc",
        "display_name": "Acme",
    }
    assert get_custom_field(payload, "fico_range") == "650-699"


def test_get_custom_field_missing_returns_none() -> None:
    payload = {"id": "lead_abc"}
    assert get_custom_field(payload, "fico_range") is None


def test_get_custom_field_unknown_aegis_name_raises() -> None:
    with pytest.raises(FieldMapError, match="unknown AEGIS-side field name"):
        get_custom_field({}, "not_a_real_field")


# ----------------------------------------------------------------------
# Entity-type normalization (Close choice → AEGIS literal)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("close_value", "expected_aegis"),
    [
        ("LLC", "llc"),
        ("C-Corp", "corp"),
        ("S-Corp", "corp"),
        ("Sole Proprietorship", "sole_prop"),
        ("Partnership", "partnership"),
        ("Non-Profit", "other"),
        ("Other", "other"),
        ("Option 1", "other"),
    ],
)
def test_normalize_entity_type_every_close_choice(close_value: str, expected_aegis: str) -> None:
    assert normalize_entity_type(close_value) == expected_aegis


def test_normalize_entity_type_covers_every_table_entry() -> None:
    """If Close adds a new Entity type choice in the dashboard, this
    guard fails until the parametrize list is updated."""
    assert set(CLOSE_ENTITY_TYPE_TO_AEGIS.keys()) == {
        "LLC",
        "C-Corp",
        "S-Corp",
        "Sole Proprietorship",
        "Partnership",
        "Non-Profit",
        "Other",
        "Option 1",
    }


def test_normalize_entity_type_none_returns_none() -> None:
    assert normalize_entity_type(None) is None


def test_normalize_entity_type_none_marker_returns_none() -> None:
    assert normalize_entity_type("-None-") is None


def test_normalize_entity_type_unknown_raises() -> None:
    """A Close choice we haven't mapped (e.g. operator added a new
    choice in Close without updating field_map.py) must raise."""
    with pytest.raises(FieldMapError, match="unknown Close Entity type"):
        normalize_entity_type("B-Corp")


# ----------------------------------------------------------------------
# filename_matches_statement_filter
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("April_bank_statement.pdf", True),  # 'bank' + 'statement'
        ("STMT_2025_03.pdf", True),  # case-insensitive 'stmt'
        ("eStmt_chase_05.pdf", True),  # 'estmt'
        ("driver_license.jpg", False),
        ("voided_check.pdf", False),
        ("Bank.PDF", True),  # case-insensitive
        ("", False),  # empty filename never matches
    ],
)
def test_filename_matches_statement_filter_defaults(filename: str, expected: bool) -> None:
    """Default filter set: statement, estmt, stmt, bank."""
    filters = ("statement", "estmt", "stmt", "bank")
    assert filename_matches_statement_filter(filename, filters) is expected


def test_filename_matches_statement_filter_empty_filters_returns_false() -> None:
    """Operator opt-out via empty env should not silently let everything
    through — explicit zero means zero."""
    assert filename_matches_statement_filter("statement.pdf", ()) is False


# ----------------------------------------------------------------------
# filename_is_non_statement (deny list)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected_term",
    [
        # The four cases from the 2026-06-16 prod recovery pass spec
        ("voided check.pdf", "voided"),
        ("chase_statement_march.pdf", None),
        ("drivers_license.jpg", "driver"),
        ("bank_statement_2026.pdf", None),
        # Each deny term — every entry in NON_STATEMENT_FILENAME_TERMS
        # must show up here so a future edit of the constant can't
        # silently lose a test
        ("voided_check_2026.pdf", "voided"),
        ("Acme - Void Check - Cover Page.pdf", "void check"),
        ("Driver License Front.pdf", "driver"),
        ("operator_license_scan.pdf", "license"),
        ("signed_contract_aegis_v2.pdf", "contract"),
        ("Funding Application 2026-06-12.pdf", "application"),
        ("Wyoming Bylaws Vu Development.pdf", "bylaws"),
        ("2024 Tax Return Documents.pdf", "tax return"),
        ("Q1 2026 Balance Sheet.pdf", "balance sheet"),
        ("Q1 2026 P&L.pdf", "p&l"),
        ("Profit and Loss Statement.pdf", "profit"),
        ("invoice_20260315.pdf", "invoice"),
        ("operator W-2 2024.pdf", "w-2"),
        ("operator_1099_2024.pdf", "1099"),
        # "contract" is checked before "signed" in the deny list, so
        # ``filename_is_non_statement`` returns the first match in
        # iteration order. Both terms would match this filename; the
        # audit / CSV cell only needs one to surface the reason.
        ("Apollo signed contract fully executed.pdf", "contract"),
        ("merchant_agreement_v3.pdf", "agreement"),
        ("addendum_2026-03.pdf", "addendum"),
        ("amendment_to_terms.pdf", "amendment"),
        # Case-insensitive
        ("VOIDED CHECK.PDF", "voided"),
        ("DRIVERS_LICENSE.JPG", "driver"),
        # Empty filename never matches anything
        ("", None),
    ],
)
def test_filename_is_non_statement(filename: str, expected_term: str | None) -> None:
    """The deny-list filter MUST reject every operator-surfaced
    non-statement filename type and MUST pass clean bank-statement
    filenames. Both the recovery script and the webhook orchestration
    short-circuit on a non-None return.
    """
    assert filename_is_non_statement(filename) == expected_term


def test_filename_is_non_statement_does_not_short_circuit_clean_statements() -> None:
    """Every known clean filename shape passes the deny filter (returns
    None). Belt-and-suspenders for ``filename_matches_statement_filter``
    callers that compose both checks.
    """
    clean_names = (
        "April_bank_statement.pdf",
        "STMT_2025_03.pdf",
        "eStmt_chase_05.pdf",
        "chase_statement_march.pdf",
        "bank_statement_2026.pdf",
        "Bank.PDF",
        "2025-04_KYC_bank_statement.pdf",
        "acme_stmt_apr.pdf",
    )
    for name in clean_names:
        assert filename_is_non_statement(name) is None, (
            f"clean filename {name!r} matched a deny term"
        )
