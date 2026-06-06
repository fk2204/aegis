"""Dictionary of counterparty patterns — the deterministic first pass.

Each entry is a regex against the transaction description string, paired
with the counterparty class it implies and a short reason token. Order
matters: most-specific patterns first, generic fallbacks last. The
first match wins.

ADDING A NEW PATTERN — workflow.
================================

When the operator finds an ``unknown`` row in a real parse that should
have classified deterministically (a new processor, a regional bank's
transfer description shape, etc.), the addition path is:

1. Add a tuple to ``COUNTERPARTY_PATTERNS`` below, keeping the
   most-specific-first ordering.
2. Verify it doesn't shadow earlier rules — add a fixture row
   under ``tests/counterparty/fixtures/`` from a REAL captured
   description (CLAUDE.md "external-integration test discipline"
   applies here: never hand-write a fake description; capture the
   real bytes from a prod parse).
3. Run ``make test`` to confirm the new fixture classifies correctly
   AND that the existing fixtures still classify as before.
4. Optionally run ``scripts/reclassify_counterparties.py`` against
   prod analyses to re-label historical rows without re-parsing the
   PDFs.

DO NOT promote an LLM-suggested label into this dictionary without
operator review. The dictionary is the auditable, deterministic
baseline; LLM-assist is the assist layer that surfaces for review,
not a label-creation autopilot.
"""

from __future__ import annotations

import re
from typing import Final

# Each entry: (compiled_pattern, counterparty_class, reason, confidence,
#              direction_hint)
#
# direction_hint:
#   "incoming"  → pattern only fires if amount >= 0 (revenue side)
#   "outgoing"  → pattern only fires if amount <  0 (expense / outflow)
#   "either"    → fires regardless of sign
#
# The "own_account_or_unconfirmed" placeholder is replaced by the bundle
# matcher with either "own_account" (matched both sides) or
# "own_account_unconfirmed" (singleton in the bundle). It is NOT a
# user-facing class — see ``classify._PLACEHOLDER_OWN_TRANSFER``.

# A small alias so the placeholder shows up as the same string the
# matcher consumes without importing across modules.
_PLACEHOLDER_OWN_TRANSFER: Final[str] = "_own_transfer_unresolved"


_PATTERNS_RAW: tuple[tuple[str, str, str, int, str], ...] = (
    # ──────────────────────────────────────────────────────────────────
    # OWN-ACCOUNT TRANSFERS — placeholder; resolved by the bundle matcher
    # ──────────────────────────────────────────────────────────────────
    #
    # Bank of America's online-banking transfer description shape:
    #   "Online Banking transfer to CHK 7722 Confirmation# 5616819490"
    #   "Online Banking transfer from CHK 7719 Confirmation# 4556990395"
    # Same Confirmation# appears on BOTH sides of the pair when both
    # accounts are at BoA — verified empirically against VU's 5-doc
    # bundle on 2026-06-05 (7 pairs, 0 false matches).
    (
        r"Online Banking transfer\s+(?:to|from)\s+(?:CHK|SAV)\s+\d+",
        _PLACEHOLDER_OWN_TRANSFER, "boa_online_transfer", 100, "either",
    ),
    # Lowercase / non-"Banking" variant — same shape, different bank or
    # BoA's product copy variation. Verified 2026-06-05 against VU's
    # CHK 1218 row: "Online transfer from CHK 1218 Confirmation#
    # fkl3mwkij; MMA SPECIA". CHK 1218 isn't in VU's bundle so the
    # bundle matcher resolves this as own_account_unconfirmed
    # (no_statement). The MORE-specific "Online Banking transfer"
    # above wins on BoA's standard description; this fires only when
    # "Banking" is absent.
    (
        r"\bOnline transfer\s+(?:to|from)\s+(?:CHK|SAV)\s+\d+",
        _PLACEHOLDER_OWN_TRANSFER, "generic_online_transfer", 85, "either",
    ),
    # Chase-style transfer description (anecdotal; promote to verified
    # once we have a real Chase fixture).
    (
        r"REMOTE ONLINE TRANSFER",
        _PLACEHOLDER_OWN_TRANSFER, "chase_remote_transfer", 85, "either",
    ),

    # ──────────────────────────────────────────────────────────────────
    # BOOK WIRES — direct class; NOT routed through the bundle matcher
    # ──────────────────────────────────────────────────────────────────
    #
    # "WIRE TYPE:BOOK IN/OUT DATE:… TIME:… ET TRN:… …" — a BoA book
    # wire identified only by a TRN: tracking number. No CHK/SAV
    # reference, no Confirmation#, so the bundle matcher has nothing
    # to pair against and the description alone cannot tell us
    # internal-vs-external.
    #
    # This is the load-bearing fix from the 2026-06-05 unknown-bucket
    # audit: 15 such rows on VU totalled ~$1.97M (13 incoming +
    # 2 outgoing). Routing them to ``_PLACEHOLDER_OWN_TRANSFER`` made
    # the matcher fall through to ``not_transfer`` → ``unknown``,
    # silently hiding $1.5M of incoming flow as expense-shaped noise.
    # Now they land in a dedicated ``book_wire_unresolved`` class:
    # NOT counted as revenue, NOT netted as own_account, held for
    # operator resolution.
    (
        r"WIRE TYPE:BOOK\s+(?:IN|OUT)",
        "book_wire_unresolved", "book_wire_trn_only", 90, "either",
    ),

    # ──────────────────────────────────────────────────────────────────
    # CARD PAYDOWN — paying down a credit card (NOT an own_account move)
    # ──────────────────────────────────────────────────────────────────
    #
    # BoA "Online Banking payment to CRD 0993 Confirmation# …".
    # The CRD prefix is the disambiguator vs CHK/SAV (own_account).
    (
        r"Online Banking payment\s+to\s+CRD\s+\d+",
        "card_paydown", "boa_crd_paydown", 100, "outgoing",
    ),
    # Generic CC payment phrasings.
    (
        r"\bCC PAYMENT\b|CREDIT CARD PAYMENT|CARD PAYMENT",
        "card_paydown", "generic_card_payment", 80, "outgoing",
    ),

    # ──────────────────────────────────────────────────────────────────
    # INTERNATIONAL — incoming international wire
    # ──────────────────────────────────────────────────────────────────
    #
    # BoA international ACH wire — "INTERNATIONAL WH DES:SENDER ID:…
    # INDN:<merchant name>". Verified against VU's 3 incoming wires
    # ($99.5K / $100K / $125K).
    (
        r"INTERNATIONAL\s+W[HI]\b",
        "international_client", "boa_international_wire", 95, "incoming",
    ),
    (
        r"FOREIGN INWARD",
        "international_client", "foreign_inward_wire", 90, "incoming",
    ),

    # ──────────────────────────────────────────────────────────────────
    # PROCESSORS — payment rails / e-commerce gateways
    # ──────────────────────────────────────────────────────────────────
    #
    # WooPayments (WooCommerce). Verified against VU.
    (
        r"\bWooPayments\b|WOOPAY",
        "processor", "woocommerce", 95, "incoming",
    ),
    (
        r"\bSHOPIFY\b",
        "processor", "shopify", 95, "incoming",
    ),
    (
        r"\bSTRIPE\b(?!\s+CHARGEBACK)",  # Stripe chargeback is its own category upstream
        "processor", "stripe", 95, "incoming",
    ),
    (
        r"\bPAYPAL\b|\bPP\*",
        "processor", "paypal", 90, "incoming",
    ),
    # SQUARE INC is the funder name; SQ* prefix on CHECKCARD is a
    # square seller (also processor-routed).
    (
        r"\bSQUARE\s+INC\b|\bSQ\s*\*",
        "processor", "square", 90, "incoming",
    ),
    (
        r"\bCLOVER\b|CLOVER NETWORK",
        "processor", "clover", 90, "incoming",
    ),
    (
        r"\bAMAZON PAY\b|AMAZONPAY",
        "processor", "amazon_pay", 90, "incoming",
    ),
    # CPS MERCHANT SER = Card Processing Services merchant fee. The
    # merchant is paying the processor for processing — so for the
    # MERCHANT this is an expense to a processor. Direction matters.
    (
        r"CPS MERCHANT SER",
        "processor", "cps_merchant_fee", 70, "either",
    ),

    # ──────────────────────────────────────────────────────────────────
    # END CUSTOMER — named business/individual via Zelle, named ACH, wire
    # ──────────────────────────────────────────────────────────────────
    #
    # Zelle INCOMING (named sender). "Zelle payment from LLC DRIVE A…",
    # "Zelle payment from SHAHRAN VAL…". The sender name is the
    # concentration signal.
    (
        r"Zelle payment from\s+",
        "end_customer", "zelle_incoming", 85, "incoming",
    ),
    # Zelle OUTGOING — could be an end customer (refund) or own
    # account / vendor. Default to unknown; operator decides.
    (
        r"Zelle payment to\s+",
        "unknown", "zelle_outgoing_review", 50, "outgoing",
    ),

    # ──────────────────────────────────────────────────────────────────
    # KNOWN NOISE — fees, verifications, statement items
    # ──────────────────────────────────────────────────────────────────
    #
    # Venmo cashout — the merchant pulling processed revenue from
    # Venmo to their bank account. P2P payment rail; counts as a
    # processor for revenue purposes. Distinct from VENMO DES:ACCTVERIFY
    # (below) which is just account-verification micro-deposit noise.
    # MUST appear before ACCTVERIFY to win the more-specific match.
    (
        r"VENMO\s+DES:CASHOUT",
        "processor", "venmo_cashout", 90, "incoming",
    ),
    # Venmo account-verification micro-deposits. Not revenue.
    (
        r"VENMO\s+DES:ACCTVERIFY",
        "unknown", "venmo_acctverify", 30, "either",
    ),
    # BoA preferred-rewards credit (a fee waiver / rebate).
    (
        r"Prfd\s+Rwds\s+for\s+Bus-Book",
        "unknown", "boa_preferred_rewards", 40, "incoming",
    ),
    # Generic CHECKCARD / PURCHASE — the merchant SPENDING with a debit
    # card. Expense; not in the 7 counterparty categories. Operator
    # review surfaces these as unknown so they don't accidentally fall
    # into "end_customer" or "processor".
    (
        r"\bCHECKCARD\s+\d",
        "unknown", "card_purchase_outgoing", 40, "outgoing",
    ),
    (
        r"^PURCHASE\s+\d",
        "unknown", "card_purchase_outgoing", 40, "outgoing",
    ),

    # ──────────────────────────────────────────────────────────────────
    # CLAIMS / INSURANCE / NAMED-ACH — heuristics
    # ──────────────────────────────────────────────────────────────────
    #
    # "CLAIMS PROCESSING TRANSACTION" — insurance claim payout to the
    # merchant. The payer's identity is in the ID/INDN tokens. Tag as
    # end_customer for now; operator may want a separate category.
    (
        r"CLAIMS PROCESSING TRANSACTION",
        "end_customer", "insurance_claim_payout", 60, "incoming",
    ),
)


# Compile once at import time. Public ordering matters; expose the
# compiled list so tests can introspect rule order.
COUNTERPARTY_PATTERNS: Final[
    tuple[tuple[re.Pattern[str], str, str, int, str], ...]
] = tuple(
    (re.compile(pat, re.IGNORECASE), cls, reason, conf, direction)
    for pat, cls, reason, conf, direction in _PATTERNS_RAW
)


def lookup_dictionary(
    description: str, amount_sign: int
) -> tuple[str, str, int] | None:
    """Run the description through the dictionary. Return
    (counterparty_or_placeholder, reason, confidence) or None if no
    rule matched.

    ``amount_sign`` is +1 for positive amounts (incoming), -1 for
    negative (outgoing), 0 for zero. Rules whose ``direction`` is
    "incoming" only fire for sign +1; "outgoing" only for -1;
    "either" fires regardless. Direction filtering is what keeps an
    "Amazon" outgoing CHECKCARD purchase from being mis-tagged as a
    revenue processor.

    Note: the returned counterparty token MAY be
    ``_PLACEHOLDER_OWN_TRANSFER`` — callers must resolve that via the
    bundle matcher before producing a final ``CounterpartyClass``.
    """
    text = description.strip()
    if not text:
        return None
    for pat, cls, reason, conf, direction in COUNTERPARTY_PATTERNS:
        if direction == "incoming" and amount_sign <= 0:
            continue
        if direction == "outgoing" and amount_sign >= 0:
            continue
        if pat.search(text):
            return cls, reason, conf
    return None


# Other-account-last-4 extractor. Patterns like:
#   "Online Banking transfer to CHK 7722 Confirmation# …" → "7722"
#   "Online Banking payment to CRD 0993 Confirmation# …"  → "0993"
#   "WIRE TYPE:BOOK IN DATE:… <…> …"                       → None (no acct in line)
_OTHER_ACCOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:CHK|SAV|CRD)\s+(\d{3,12})\b"
)


def extract_other_account_last4(description: str) -> str | None:
    """Pull the OTHER account's last-4 from the description, or None."""
    m = _OTHER_ACCOUNT_RE.search(description)
    return m.group(1) if m else None


# Confirmation# extractor. BoA's "Online Banking transfer" uses a
# purely numeric Confirmation# (e.g. "Confirmation# 4556990395"); the
# generic "Online transfer" variant uses an alphanumeric one (e.g.
# "Confirmation# fkl3mwkij"). Accept either so the bundle matcher can
# pair across both shapes.
_CONFIRMATION_RE: Final[re.Pattern[str]] = re.compile(
    r"Confirmation#\s*([a-zA-Z0-9]+)", re.IGNORECASE
)


def extract_confirmation_number(description: str) -> str | None:
    """Pull a Confirmation# token from the description, or None."""
    m = _CONFIRMATION_RE.search(description)
    return m.group(1) if m else None


__all__ = [
    "COUNTERPARTY_PATTERNS",
    "extract_confirmation_number",
    "extract_other_account_last4",
    "lookup_dictionary",
]
