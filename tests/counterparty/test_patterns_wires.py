"""Counterparty dictionary — incoming-wire patterns (P7b, 2026-07-01).

WIRE IN, WIRE INCOMING, FEDWIRE CREDIT, DOMESTIC WIRE CREDIT
patterns added at the bottom of COUNTERPARTY_PATTERNS. These are
distinct from BoA's "WIRE TYPE:BOOK IN" (book_wire_unresolved,
covered by the 2026-06-05 unknown-bucket audit) — those internal
book wires have no external counterparty.

Each test uses a real description shape observed in the operator's
statement corpus (verified against Turnbull's Truist incoming wires
+ VU's Chase FEDWIRE CREDIT lines).
"""

from __future__ import annotations

import pytest

from aegis.counterparty.patterns import lookup_dictionary


@pytest.mark.parametrize(
    "description",
    [
        "WIRE IN 20260616MMQFMPQKM000123 CUSTOMER: ACME INDUSTRIES INC",
        "WIRE INCOMING FROM: BANK OF AMERICA REF: 20260616AA",
        "WIRE IN 20260618 REF ACME INDUSTRIES",
    ],
)
def test_wire_incoming_classifies_as_end_customer(description: str) -> None:
    result = lookup_dictionary(description, amount_sign=1)
    assert result is not None, f"expected match for: {description!r}"
    cls, reason, conf = result
    assert cls == "end_customer"
    assert reason == "wire_incoming_generic"
    assert conf == 65


@pytest.mark.parametrize(
    "description",
    [
        "FEDWIRE CREDIT VIA: JPMORGAN CHASE BANK B/O: ACME CO",
        "FEDWIRE CREDIT 20260620 REF: 5566",
    ],
)
def test_fedwire_credit_classifies_as_end_customer(description: str) -> None:
    result = lookup_dictionary(description, amount_sign=1)
    assert result is not None
    cls, reason, conf = result
    assert cls == "end_customer"
    assert reason == "fedwire_credit"
    assert conf == 70


@pytest.mark.parametrize(
    "description",
    [
        "DOMESTIC WIRE CREDIT REF: 20260618 CUST: BIG BOX CORP",
        "DOMESTIC WIRE CREDIT FROM ACME CO",
    ],
)
def test_domestic_wire_credit_classifies_as_end_customer(description: str) -> None:
    result = lookup_dictionary(description, amount_sign=1)
    assert result is not None
    cls, reason, conf = result
    assert cls == "end_customer"
    assert reason == "domestic_wire_credit"
    assert conf == 70


def test_boa_book_wire_still_routes_to_book_wire_unresolved() -> None:
    """The 2026-06-05 book-wire fix must not be overridden by the
    new generic WIRE-IN patterns. Order in COUNTERPARTY_PATTERNS
    matters: book_wire rule fires before wire_incoming_generic."""
    description = "WIRE TYPE:BOOK IN DATE:2026-06-15 TIME:1234 ET TRN:20260615CH99"
    result = lookup_dictionary(description, amount_sign=1)
    assert result is not None
    cls, reason, _ = result
    assert cls == "book_wire_unresolved"
    assert reason == "book_wire_trn_only"


def test_wire_incoming_does_not_fire_on_outgoing_amounts() -> None:
    """Direction-gated: 'incoming' rules only fire when amount_sign=+1.
    Prevents an outgoing 'wire out' from being tagged as revenue."""
    result = lookup_dictionary("WIRE IN 20260618 REF ACME", amount_sign=-1)
    # This description WOULD match the incoming rule; direction gate
    # should suppress. If any other rule fires that's fine, but the
    # incoming-generic rule specifically must not.
    assert result is None or result[1] != "wire_incoming_generic"
