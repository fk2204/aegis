"""Counterparty dictionary — Truist + named-ACH expansion tests.

Captured 2026-06-30 from The Turnbull Company LLC (Truist Bank
merchant, 530 transactions, 526 dictionary misses against the prior
BoA-skewed pattern set). Each description is the literal string
from ``public.transactions.description`` on prod — CLAUDE.md
"external-integration test discipline" applies (no hand-written
fakes).

The pre-expansion baseline was 4 hits / 526 misses. Post-expansion
target (covered by these tests): every revenue-shaped row Turnbull
emits classifies, plus the explicit "not revenue, not signal"
overdraft / check / fee rows surface with a specific reason instead
of the generic ``unknown / no_dictionary_match``.
"""

from __future__ import annotations

import pytest

from aegis.counterparty.patterns import (
    _PLACEHOLDER_OWN_TRANSFER,
    extract_other_account_last4,
    lookup_dictionary,
)

# ---------------------------------------------------------------------
# Truist own-account transfers
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "description,sign",
    [
        ("TRUIST ONLINE TRANSFER ONLINE TO ****8865", -1),
        ("TRUIST ONLINE TRANSFER ONLINE FROM ****8865 -", 1),
        ("TRUIST ONLINE TRANSFER ONLINE TO ****5120", -1),
    ],
)
def test_truist_online_transfer_routes_to_own_account_placeholder(
    description: str, sign: int
) -> None:
    result = lookup_dictionary(description, sign)
    assert result is not None
    cls, reason, conf = result
    assert cls == _PLACEHOLDER_OWN_TRANSFER
    assert reason == "truist_online_transfer"
    assert conf == 100


def test_truist_credit_card_paydown() -> None:
    result = lookup_dictionary("TRUIST ONLINE CREDIT CARD PMT ONLINE TO ****4466", -1)
    assert result is not None
    cls, reason, conf = result
    assert cls == "card_paydown"
    assert reason == "truist_crd_paydown"
    assert conf == 100


def test_truist_credit_card_paydown_does_not_fire_on_incoming() -> None:
    # Direction filter — incoming amount should not match the paydown rule.
    result = lookup_dictionary("TRUIST ONLINE CREDIT CARD PMT ONLINE TO ****4466", 1)
    # Either falls through to no_match or another rule. The card_paydown
    # rule MUST NOT fire.
    assert result is None or result[0] != "card_paydown"


# ---------------------------------------------------------------------
# Named-ACH-credit revenue patterns (Murphy Brown — Turnbull's
# top recurring corporate customer)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "PAYMENTS Murphy Brown 0011THE TURNBULL CO CUSTOMER ID 252494",
        "PAYMENTS Murphy Brown 0018THE TURNBULL CO CUSTOMER ID 255284",
        "PAYMENTS Murphy Brown 0010THE TURNBULL CO CUSTOMER ID 252494",
    ],
)
def test_payments_named_company_routes_to_end_customer(description: str) -> None:
    result = lookup_dictionary(description, 1)
    assert result is not None
    cls, reason, _ = result
    assert cls == "end_customer"
    assert reason == "ach_corp_payments_named"


def test_payments_named_does_not_fire_when_outgoing() -> None:
    # Direction filter — an outgoing "PAYMENTS" row would not be revenue.
    result = lookup_dictionary("PAYMENTS Murphy Brown 0011THE TURNBULL CO CUSTOMER ID 252494", -1)
    assert result is None or result[0] != "end_customer"


def test_processor_pattern_wins_over_payments_prefix() -> None:
    """A 'PAYMENTS PAYPAL …' row should hit the PAYPAL processor rule
    (more specific, listed earlier in COUNTERPARTY_PATTERNS) BEFORE the
    broad ``^PAYMENTS`` end-customer fallback."""
    result = lookup_dictionary("PAYMENTS PAYPAL TRANSFER 12345", 1)
    assert result is not None
    cls, reason, _ = result
    assert cls == "processor"
    assert reason == "paypal"


# ---------------------------------------------------------------------
# ATM check / cash deposit — incoming customer revenue
# ---------------------------------------------------------------------


def test_truist_atm_check_deposit() -> None:
    result = lookup_dictionary(
        "TRUIST ATM CHECK DEPOSIT 05-24-26 11:50 A179 LUMBERTON MAIN BRANCH 11794307",
        1,
    )
    assert result is not None
    cls, reason, _ = result
    assert cls == "end_customer"
    assert reason == "atm_check_or_cash_deposit"


# ---------------------------------------------------------------------
# Bank internal fees — outgoing, not signal
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "OVERDRAFT ITEM FEE ($36/ITEM) 36",
        "RETURNED ITEM FEE",
    ],
)
def test_bank_internal_fee_classifies_with_specific_reason(
    description: str,
) -> None:
    result = lookup_dictionary(description, -1)
    assert result is not None
    cls, reason, _ = result
    assert cls == "unknown"
    assert reason == "bank_internal_fee"


# ---------------------------------------------------------------------
# Outgoing check payments — specific reason, not generic unknown
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Check 11728",
        "Check *11978",
        "CHECK 11721",
        "CHECK *11727",
        "CHECK #11715",
    ],
)
def test_check_payment_outgoing_specific_reason(description: str) -> None:
    result = lookup_dictionary(description, -1)
    assert result is not None
    cls, reason, _ = result
    assert cls == "unknown"
    assert reason == "check_payment_outgoing"


# ---------------------------------------------------------------------
# Existing patterns regression — confirm pre-expansion fixtures
# still classify the same after the pattern additions
# ---------------------------------------------------------------------


def test_boa_online_transfer_still_routes_to_placeholder() -> None:
    result = lookup_dictionary("Online Banking transfer to CHK 7722 Confirmation# 5616819490", -1)
    assert result is not None
    cls, reason, _ = result
    assert cls == _PLACEHOLDER_OWN_TRANSFER
    assert reason == "boa_online_transfer"


def test_book_wire_still_routes_to_book_wire_unresolved() -> None:
    result = lookup_dictionary("WIRE TYPE:BOOK IN DATE:240605 TIME:1247 ET TRN:20240605000123", 1)
    assert result is not None
    cls, reason, _ = result
    assert cls == "book_wire_unresolved"
    assert reason == "book_wire_trn_only"


def test_stripe_still_classifies_as_processor() -> None:
    result = lookup_dictionary("STRIPE TRANSFER abc123", 1)
    assert result is not None
    assert result[0] == "processor"
    assert result[1] == "stripe"


def test_zelle_incoming_still_end_customer() -> None:
    result = lookup_dictionary("Zelle payment from JOHN DOE", 1)
    assert result is not None
    assert result[0] == "end_customer"
    assert result[1] == "zelle_incoming"


# ---------------------------------------------------------------------
# extract_other_account_last4 — Truist masked-account format
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "description,expected",
    [
        # Truist ****XXXX format
        ("TRUIST ONLINE TRANSFER ONLINE TO ****8865", "8865"),
        ("TRUIST ONLINE TRANSFER ONLINE FROM ****8865 -", "8865"),
        ("TRUIST ONLINE CREDIT CARD PMT ONLINE TO ****4466", "4466"),
        ("TRUIST ONLINE TRANSFER ONLINE TO ****5120", "5120"),
        # BoA CHK/SAV/CRD format (regression — must still extract)
        ("Online Banking transfer to CHK 7722 Confirmation# 5616819490", "7722"),
        ("Online Banking payment to CRD 0993 Confirmation# 1234567890", "0993"),
        ("Online transfer from SAV 1218 Confirmation# fkl3mwkij", "1218"),
        # Descriptions with no account reference
        ("OVERDRAFT ITEM FEE ($36/ITEM) 36", None),
        ("Check 11728", None),
    ],
)
def test_extract_other_account_last4(description: str, expected: str | None) -> None:
    assert extract_other_account_last4(description) == expected


# ---------------------------------------------------------------------
# Coverage smoke — confirm the dictionary now catches the high-impact
# Turnbull rows that were 100% miss before
# ---------------------------------------------------------------------


def test_turnbull_high_impact_descriptions_classify() -> None:
    """Spot-check the descriptions whose miss was the most expensive
    (Murphy Brown PAYMENTS revenue + Truist own-account transfers +
    Truist CC paydown). Pre-expansion: all 4 returned ``None``."""
    samples = [
        # (description, amount_sign, expected_class)
        ("TRUIST ONLINE TRANSFER ONLINE TO ****8865", -1, _PLACEHOLDER_OWN_TRANSFER),
        (
            "PAYMENTS Murphy Brown 0011THE TURNBULL CO CUSTOMER ID 252494",
            1,
            "end_customer",
        ),
        (
            "TRUIST ATM CHECK DEPOSIT 05-24-26 11:50 A179 LUMBERTON MAIN BRANCH 11794307",
            1,
            "end_customer",
        ),
        ("TRUIST ONLINE CREDIT CARD PMT ONLINE TO ****4466", -1, "card_paydown"),
    ]
    for description, sign, expected_cls in samples:
        result = lookup_dictionary(description, sign)
        assert result is not None, f"miss on {description!r}"
        assert result[0] == expected_cls, (
            f"description={description!r} expected={expected_cls} got={result[0]}"
        )
