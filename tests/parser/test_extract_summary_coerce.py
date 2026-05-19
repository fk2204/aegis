"""Unit tests for ``_coerce_summary`` — placeholder string + Brex edge cases.

Two regression families live here:

1. **Brex-style null balances** (2026-04 incident): Brex statements render
   the period's beginning balance as a dash when the account starts at
   $0 / fresh. Claude correctly extracts that as ``null``, but the older
   code path passed None through to the Pydantic ``Money`` field which
   rejects it, blocking the whole parse.

2. **LLM placeholder strings for Optional fields** (2026-05-19 incident,
   verify-bedrock baseline run ``bf258kqym``): the Bedrock corpus run
   surfaced 7 PDFs where the LLM emitted ``"unknown"`` for
   ``summary.account_last4`` instead of ``null``. ``account_last4`` has
   ``max_length=4`` so the 7-character string fails Pydantic validation
   and the whole extraction is rejected. The same placeholder convention
   on the unconstrained ``bank_name`` / ``account_holder`` fields would
   pass Pydantic but silently corrupt downstream bundling queries +
   merchant-detail display.

   The 7 PDFs from the baseline run that surfaced this:
     - clean_profitable_boa_business_20001.pdf
     - clean_profitable_wells_fargo_business_30001.pdf
     - declining_revenue_wells_fargo_business_30007.pdf
     - math_tampered_wells_fargo_business_30004.pdf
     - metadata_tampered_wells_fargo_business_30013.pdf
     - nsf_heavy_wells_fargo_business_30002.pdf
     - preloan_spike_wells_fargo_business_30010.pdf

   The unit tests below replay the canned LLM payload shape these PDFs
   produced (``account_last4="unknown"``) against ``_coerce_summary``
   without needing a live LLM call. Future verify-bedrock runs against
   the synthetic corpus exercise the end-to-end path through real
   Bedrock; these tests catch the bug at ``make test``.
"""

from __future__ import annotations

import pytest

from aegis.parser.extract import _coerce_optional_string, _coerce_summary


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


# ---------------------------------------------------------------------------
# Placeholder string coercion — _coerce_optional_string + _coerce_summary
# Regression for verify-bedrock baseline run bf258kqym (2026-05-19).
# ---------------------------------------------------------------------------


_PLACEHOLDER_STRINGS = [
    "unknown",
    "Unknown",
    "UNKNOWN",
    "n/a",
    "N/A",
    "na",
    "NA",
    "none",
    "None",
    "null",
    "tbd",
    "TBD",
    "not available",
    "not visible",
    "not provided",
    "not specified",
    "see above",
    "-",
    "--",
    "",
    "   ",
    "\t",
    " unknown ",  # leading/trailing whitespace must still be normalized.
    " N/A\t",
]


@pytest.mark.parametrize("placeholder", _PLACEHOLDER_STRINGS)
def test_coerce_optional_string_normalizes_placeholders_to_none(
    placeholder: str,
) -> None:
    """Every known placeholder convention → None, regardless of case / whitespace."""
    assert _coerce_optional_string(placeholder) is None


def test_coerce_optional_string_preserves_legitimate_values() -> None:
    """Real string values pass through (with whitespace stripped)."""
    assert _coerce_optional_string("Wells Fargo") == "Wells Fargo"
    assert _coerce_optional_string("  Wells Fargo  ") == "Wells Fargo"
    assert _coerce_optional_string("Acme Corp LLC") == "Acme Corp LLC"
    assert _coerce_optional_string("1234") == "1234"
    assert _coerce_optional_string("abcd") == "abcd"
    # Short non-placeholder string — even a single ambiguous-looking letter is preserved.
    assert _coerce_optional_string("X") == "X"


def test_coerce_optional_string_passes_non_strings_through() -> None:
    """None / non-string inputs are idempotent — helper is safe to apply broadly."""
    assert _coerce_optional_string(None) is None
    assert _coerce_optional_string(0) == 0
    assert _coerce_optional_string(["unknown"]) == ["unknown"]
    assert _coerce_optional_string({"a": 1}) == {"a": 1}


@pytest.mark.parametrize(
    "field", ["bank_name", "account_holder", "account_last4"]
)
@pytest.mark.parametrize("placeholder", ["unknown", "Unknown", "N/A", "", "  "])
def test_coerce_summary_strips_placeholders_from_optional_string_fields(
    field: str, placeholder: str
) -> None:
    """The three Optional string fields on StatementSummary all get the same coercion.

    Replays the exact shape of the LLM payload that failed in
    verify-bedrock run bf258kqym — ``summary.account_last4="unknown"`` —
    and asserts coercion produces None across all three fields and all
    common placeholder values.
    """
    payload = {
        field: placeholder,
        "beginning_balance": 100.00,
        "ending_balance": 200.00,
        "deposit_total": 500.00,
        "withdrawal_total": 400.00,
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
    }
    out = _coerce_summary(payload)
    assert out[field] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("bank_name", "Wells Fargo"),
        ("bank_name", "Bank of America"),
        ("account_holder", "Acme Corp LLC"),
        ("account_holder", "Jane Doe"),
        ("account_last4", "1234"),
        ("account_last4", "9876"),
        ("account_last4", "abcd"),  # 4-char string passes max_length boundary.
    ],
)
def test_coerce_summary_preserves_legitimate_optional_strings(
    field: str, value: str
) -> None:
    """Real values for the three Optional string fields pass through unchanged."""
    payload = {
        field: value,
        "beginning_balance": 100.00,
        "ending_balance": 200.00,
        "deposit_total": 500.00,
        "withdrawal_total": 400.00,
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
    }
    out = _coerce_summary(payload)
    assert out[field] == value


def test_coerce_summary_account_last4_boundary_5_chars_not_coerced() -> None:
    """A 5-char non-placeholder string is preserved (so Pydantic still rejects it).

    We deliberately do NOT relax the ``max_length=4`` constraint —
    coercion only normalizes known placeholder *strings* to None.
    A genuinely-too-long value like "12345" must still fail validation
    downstream so the bug surfaces loudly instead of silently corrupting
    the DB ``CHAR(4)`` column.
    """
    payload = {
        "account_last4": "12345",
        "beginning_balance": 100.00,
        "ending_balance": 200.00,
        "deposit_total": 500.00,
        "withdrawal_total": 400.00,
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
    }
    out = _coerce_summary(payload)
    assert out["account_last4"] == "12345"  # passed through; Pydantic will reject it.


def test_coerce_summary_end_to_end_pydantic_validates_with_placeholder() -> None:
    """Full path: LLM-style payload with placeholder strings → ExtractedStatement.

    Mirrors what the corpus run bf258kqym produced for the 7 failing PDFs:
    a complete summary block where account_last4="unknown". With the
    coercion layer in place, the StatementSummary validates cleanly.
    """
    from aegis.parser.models import StatementSummary

    payload = {
        "bank_name": "Wells Fargo",
        "account_holder": "unknown",
        "account_last4": "unknown",
        "beginning_balance": 1000.00,
        "ending_balance": 2000.00,
        "deposit_total": 5000.00,
        "withdrawal_total": 4000.00,
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
    }
    coerced = _coerce_summary(payload)
    # Re-do the date fields (Pydantic will coerce ISO strings to date).
    summary = StatementSummary.model_validate(coerced)
    assert summary.bank_name == "Wells Fargo"
    assert summary.account_holder is None
    assert summary.account_last4 is None
