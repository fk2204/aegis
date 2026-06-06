"""Counterparty classifier — REAL VU Development bundle acceptance test.

CLAUDE.md "external-integration test discipline" applies: this fixture
is captured from the live ``transactions`` table on 2026-06-05 (144
real rows across 5 documents: CHK 7722 March + CHK 7719 Feb/Mar/Apr/May).
NOT a hand-written sample. The classifier is graded against the
descriptions Bank of America actually emits.

The acceptance criteria (from the operator's spec):

* CHK 7722 ↔ CHK 7719 transfers that have BOTH sides in the bundle
  pair up cleanly via Confirmation# → both sides classify as
  ``own_account`` with each other as ``paired_transaction_id``.
* CHK 7722 references on 7719's Feb/Apr/May statements (no matching
  7722 statement for those periods) classify as
  ``own_account_unconfirmed`` with reason ``missing_period_for_known_account``.
* The single "to CHK 9940" reference (no 9940 statement anywhere in
  the bundle) classifies as ``own_account_unconfirmed`` with reason
  ``no_statement_for_referenced_account`` — surfacing the gap the
  operator must investigate.
* "Online Banking payment to CRD 0993" classifies as ``card_paydown``,
  NOT ``own_account_unconfirmed`` (CRD prefix disambiguates).
* "INTERNATIONAL WH" credits classify as ``international_client``.
* "WooPayments" credits classify as ``processor``.
* "Zelle payment from <name>" credits classify as ``end_customer``.
* CHECKCARD / PURCHASE outgoing classify as ``unknown`` (expense, not
  in the 7-category counterparty taxonomy yet).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from aegis.counterparty import (
    BundleSummary,
    CounterpartyClassification,
    classify_bundle,
)
from aegis.counterparty.bundle_match import match_own_account_transfers
from aegis.counterparty.patterns import (
    extract_confirmation_number,
    extract_other_account_last4,
)
from aegis.parser.models import ClassifiedTransaction

_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "vu_real_txns.json"
)


def _load_fixture() -> dict[str, Any]:
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _build_transactions(
    fixture: dict[str, Any],
) -> tuple[dict[str, list[ClassifiedTransaction]], set[str]]:
    """Reconstruct ClassifiedTransaction objects from the captured
    rows. Returns (transactions_by_doc_id, bundle_account_last4s)."""
    by_doc: dict[str, list[ClassifiedTransaction]] = {}
    accounts: set[str] = set()
    for doc in fixture["documents"]:
        last4 = doc["summary"]["account_last4"]
        if last4:
            accounts.add(last4)
        txns: list[ClassifiedTransaction] = []
        for t in doc["transactions"]:
            running = t.get("running_balance")
            txns.append(
                ClassifiedTransaction(
                    id=UUID(t["id"]),
                    posted_date=date.fromisoformat(t["posted_date"]),
                    description=t["description"],
                    amount=Decimal(t["amount"]),
                    running_balance=(
                        Decimal(running) if running is not None else None
                    ),
                    source_page=1,
                    source_line=1,
                    category=t["category"],
                    classification_confidence=100,
                )
            )
        by_doc[doc["document_id"]] = txns
    return by_doc, accounts


@pytest.fixture(scope="module")
def vu_bundle() -> tuple[
    dict[str, list[ClassifiedTransaction]], set[str]
]:
    return _build_transactions(_load_fixture())


@pytest.fixture(scope="module")
def vu_classifications(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
) -> tuple[dict[UUID, CounterpartyClassification], BundleSummary]:
    txns_by_doc, accts = vu_bundle
    return classify_bundle(txns_by_doc, accts)


# ─────────────────────────────────────────────────────────────────────
# Fixture sanity
# ─────────────────────────────────────────────────────────────────────


def test_vu_fixture_has_5_documents_and_144_transactions(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
) -> None:
    by_doc, accts = vu_bundle
    assert len(by_doc) == 5
    total = sum(len(v) for v in by_doc.values())
    assert total == 144
    # 7719 and 7722 both appear as own statements in the bundle.
    assert "7719" in accts
    assert "7722" in accts


# ─────────────────────────────────────────────────────────────────────
# Bundle matcher direct — the load-bearing piece
# ─────────────────────────────────────────────────────────────────────


def test_bundle_matcher_pairs_7722_and_7719_transfers_by_confirmation_number(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
) -> None:
    """The matcher must find exactly 7 paired Confirmation#s — each
    representing a CHK 7719 ↔ CHK 7722 round-trip the operator can
    net. Numbers come from the empirical inspection on 2026-06-05."""
    txns_by_doc, accts = vu_bundle
    matches = match_own_account_transfers(txns_by_doc, accts)

    matched_pairs: set[frozenset[UUID]] = set()
    for txn_id, m in matches.items():
        if m.status == "matched" and m.paired_transaction_id:
            matched_pairs.add(
                frozenset({txn_id, m.paired_transaction_id})
            )
    assert len(matched_pairs) == 7, (
        f"expected 7 own_account pairs, got {len(matched_pairs)}"
    )


def test_bundle_matcher_flags_chk_9940_as_no_statement(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
) -> None:
    """The single 'to CHK 9940' transfer is in 7719's bundle but VU has
    no 9940 statement anywhere → no_statement (surface the gap)."""
    txns_by_doc, accts = vu_bundle
    matches = match_own_account_transfers(txns_by_doc, accts)

    chk_9940_matches = [
        m for m in matches.values() if m.other_account_last4 == "9940"
    ]
    assert len(chk_9940_matches) == 1
    assert chk_9940_matches[0].status == "no_statement"


def test_bundle_matcher_flags_7722_period_gaps_as_period_gap(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
) -> None:
    """7719's Feb/Apr/May statements reference CHK 7722 in periods we
    don't have a 7722 statement for. These classify as period_gap —
    we DO have 7722 somewhere in the bundle (March), so we know the
    account exists; we just can't net this particular transfer."""
    txns_by_doc, accts = vu_bundle
    matches = match_own_account_transfers(txns_by_doc, accts)

    period_gap_7722 = [
        m
        for m in matches.values()
        if m.other_account_last4 == "7722" and m.status == "period_gap"
    ]
    # Empirical count from 2026-06-05 inspection: 20 period-gap singletons
    # against CHK 7722.
    assert len(period_gap_7722) >= 18, (
        f"expected ~20 period-gap 7722 singletons; got {len(period_gap_7722)}"
    )


# ─────────────────────────────────────────────────────────────────────
# Full classifier outputs — acceptance against the real VU bundle
# ─────────────────────────────────────────────────────────────────────


def test_vu_acceptance_chk_9940_classifies_as_unconfirmed_no_statement(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """The operator's spec acceptance: CHK 9940 (one-sided in bundle)
    surfaces as own_account_unconfirmed with the no_statement reason."""
    classifications, _ = vu_classifications
    chk_9940 = [
        c
        for c in classifications.values()
        if c.other_account_last4 == "9940"
    ]
    assert len(chk_9940) == 1
    assert chk_9940[0].counterparty == "own_account_unconfirmed"
    assert chk_9940[0].reason == "no_statement_for_referenced_account"


def test_vu_acceptance_7722_paired_transfers_are_own_account(
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """Operator acceptance: matched pairs both sides → own_account
    with paired_transaction_id pointing across."""
    classifications, summary = vu_classifications
    own = [c for c in classifications.values() if c.counterparty == "own_account"]
    # 7 pairs x 2 sides = 14 own_account classifications.
    assert len(own) == 14
    assert summary.matched_pair_count == 7
    for c in own:
        assert c.paired_transaction_id is not None
        assert c.paired_transaction_id in classifications
        other = classifications[c.paired_transaction_id]
        assert other.counterparty == "own_account"
        assert other.paired_transaction_id == c.transaction_id


def test_vu_acceptance_crd_0993_classifies_as_card_paydown(
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """The CRD prefix must route to card_paydown, NOT own_account.
    Verifies the dictionary's most-specific-first ordering — the CRD
    rule fires before any generic transfer rule could mis-match."""
    classifications, _ = vu_classifications
    crd_0993 = [
        c
        for c in classifications.values()
        if c.other_account_last4 == "0993"
    ]
    assert len(crd_0993) >= 1
    for c in crd_0993:
        assert c.counterparty == "card_paydown", (
            f"CRD 0993 mis-classified as {c.counterparty!r}"
        )


def test_vu_acceptance_international_wires_classify_as_international_client(
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """Three INTERNATIONAL WH incoming wires ($99.5K / $100K / $125K)
    must surface as international_client."""
    classifications, _ = vu_classifications
    intl = [
        c
        for c in classifications.values()
        if c.counterparty == "international_client"
    ]
    assert len(intl) >= 3, (
        f"expected >= 3 international wires, got {len(intl)}"
    )


def test_vu_acceptance_woopayments_credits_classify_as_processor(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """WooPayments incoming credits must surface as processor (revenue
    via the WooCommerce payment rail)."""
    by_doc, _ = vu_bundle
    classifications, _ = vu_classifications
    woo_rows = [
        t
        for txns in by_doc.values()
        for t in txns
        if "WooPay" in t.description
    ]
    assert len(woo_rows) >= 2
    for t in woo_rows:
        assert classifications[t.id].counterparty == "processor"
        assert classifications[t.id].reason == "woocommerce"


def test_vu_acceptance_zelle_incoming_classifies_as_end_customer(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """'Zelle payment from <name>' = named end customer (revenue,
    genuine concentration signal)."""
    by_doc, _ = vu_bundle
    classifications, _ = vu_classifications
    zelle_in = [
        t
        for txns in by_doc.values()
        for t in txns
        if t.description.startswith("Zelle payment from")
    ]
    assert len(zelle_in) >= 4
    for t in zelle_in:
        assert classifications[t.id].counterparty == "end_customer"


def test_vu_acceptance_book_wires_classify_as_book_wire_unresolved(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """The load-bearing fix from the unknown-bucket audit.

    Every "WIRE TYPE:BOOK IN/OUT" row in VU's bundle (15 of them,
    ~$1.97M combined) must land in ``book_wire_unresolved`` —
    NOT ``unknown``, NOT ``own_account`` (matcher can't pair
    TRN-only descriptions), NOT ``own_account_unconfirmed``.
    They're held in a dedicated class for operator resolution.

    This is what kills the silent-revenue-hiding failure mode where
    13 incoming wires totalling $1.52M would be lumped with debit-
    card expenses under ``unknown``.
    """
    by_doc, _ = vu_bundle
    classifications, summary = vu_classifications
    book_rows = [
        t
        for txns in by_doc.values()
        for t in txns
        if "WIRE TYPE:BOOK" in t.description
    ]
    # Empirical count against the captured fixture: 13 BOOK IN +
    # 3 BOOK OUT = 16. (The unknown-bucket audit on 2026-06-05 cited
    # 15 because the outgoing prefix-clustering rolled by date, hiding
    # the third BOOK OUT row with a unique date prefix; the test
    # works off the real row count.)
    assert len(book_rows) == 16, (
        f"expected 16 BOOK wire rows in VU bundle, got {len(book_rows)}"
    )
    incoming = [t for t in book_rows if t.amount > 0]
    outgoing = [t for t in book_rows if t.amount < 0]
    assert len(incoming) == 13
    assert len(outgoing) == 3

    for t in book_rows:
        cc = classifications[t.id]
        assert cc.counterparty == "book_wire_unresolved", (
            f"BOOK wire {t.description[:50]!r} routed to "
            f"{cc.counterparty!r} instead of book_wire_unresolved"
        )
        assert cc.reason == "book_wire_trn_only"
        # Never paired — bundle matcher has nothing to pair against.
        assert cc.paired_transaction_id is None
    # And surfaces in the bundle summary's by_class rollup.
    assert summary.by_class.get("book_wire_unresolved") == 16


def test_vu_acceptance_venmo_cashout_classifies_as_processor(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """VENMO DES:CASHOUT rows are the merchant pulling processed
    revenue off the Venmo P2P rail — must classify as ``processor``
    with reason ``venmo_cashout``. ACCTVERIFY (different DES: value)
    stays in ``unknown`` — verified by a separate row in the bundle."""
    by_doc, _ = vu_bundle
    classifications, _ = vu_classifications

    cashout = [
        t
        for txns in by_doc.values()
        for t in txns
        if "VENMO" in t.description and "DES:CASHOUT" in t.description
    ]
    assert len(cashout) >= 3
    for t in cashout:
        cc = classifications[t.id]
        assert cc.counterparty == "processor"
        assert cc.reason == "venmo_cashout"

    acctverify = [
        t
        for txns in by_doc.values()
        for t in txns
        if "VENMO" in t.description and "DES:ACCTVERIFY" in t.description
    ]
    assert len(acctverify) >= 2
    for t in acctverify:
        # ACCTVERIFY micro-deposits remain in unknown — distinguish
        # cashout-revenue from verification-noise on the same rail.
        assert classifications[t.id].counterparty == "unknown"
        assert classifications[t.id].reason == "venmo_acctverify"


def test_vu_acceptance_lowercase_online_transfer_classifies_via_bundle_matcher(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """The lowercase 'Online transfer from CHK 1218' row in VU's bundle
    must flow through the bundle matcher. CHK 1218 isn't in our bundle
    (we have only 7719 + 7722) → own_account_unconfirmed with reason
    ``no_statement_for_referenced_account``."""
    by_doc, _ = vu_bundle
    classifications, _ = vu_classifications

    matches = [
        t
        for txns in by_doc.values()
        for t in txns
        if t.description.startswith("Online transfer ")
        and "Banking" not in t.description
    ]
    assert len(matches) >= 1
    for t in matches:
        cc = classifications[t.id]
        assert cc.counterparty == "own_account_unconfirmed"
        # CHK 1218 → no statement in bundle.
        assert cc.other_account_last4 == "1218"
        assert cc.reason == "no_statement_for_referenced_account"


def test_vu_acceptance_no_incoming_revenue_hides_in_unknown(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """The whole point of the three-pattern fix: after the patch,
    no incoming-revenue-shaped row should be sitting in ``unknown``.

    Permitted exceptions:
      - VENMO DES:ACCTVERIFY micro-deposits (tiny amounts, intentional)
      - Anything <= $5 (rounding errors, account verifications)

    Anything else incoming and unknown is a regression — Track B's
    true_revenue math would silently understate it.
    """
    from decimal import Decimal

    by_doc, _ = vu_bundle
    classifications, _ = vu_classifications

    leaks: list[ClassifiedTransaction] = []
    for txns in by_doc.values():
        for t in txns:
            cc = classifications[t.id]
            if cc.counterparty != "unknown":
                continue
            if t.amount <= 0:
                continue
            if "ACCTVERIFY" in t.description:
                continue
            if t.amount <= Decimal("5.00"):
                continue
            leaks.append(t)
    assert not leaks, (
        "incoming revenue rows leaking into unknown bucket:\n  "
        + "\n  ".join(
            f"${t.amount} :: {t.description[:80]!r}" for t in leaks
        )
    )


def test_vu_acceptance_checkcard_outgoing_classifies_as_unknown(
    vu_bundle: tuple[
        dict[str, list[ClassifiedTransaction]], set[str]
    ],
    vu_classifications: tuple[
        dict[UUID, CounterpartyClassification], BundleSummary
    ],
) -> None:
    """CHECKCARD outgoing rows (Amazon, Murphy Express, etc.) are
    expenses — not in the 7 counterparty categories yet. Surface as
    unknown so the operator decides whether to add an 'expense'
    category later or grow the dictionary differently."""
    by_doc, _ = vu_bundle
    classifications, _ = vu_classifications
    cc_outgoing = [
        t
        for txns in by_doc.values()
        for t in txns
        if t.description.startswith("CHECKCARD") and t.amount < 0
    ]
    assert len(cc_outgoing) >= 10
    bucket_counts: Counter[str] = Counter()
    for t in cc_outgoing:
        bucket_counts[classifications[t.id].counterparty] += 1
    # The dominant bucket must be unknown; we DON'T want them
    # silently landing in end_customer or processor.
    assert bucket_counts["unknown"] >= len(cc_outgoing) * 0.8, (
        f"too many CHECKCARD outgoing leaked out of unknown: {bucket_counts}"
    )


# ─────────────────────────────────────────────────────────────────────
# Helper extractor sanity
# ─────────────────────────────────────────────────────────────────────


def test_extract_other_account_last4_handles_chk_sav_crd() -> None:
    assert (
        extract_other_account_last4(
            "Online Banking transfer to CHK 7722 Confirmation# 1"
        )
        == "7722"
    )
    assert (
        extract_other_account_last4(
            "Online Banking payment to CRD 0993 Confirmation# 1"
        )
        == "0993"
    )
    assert (
        extract_other_account_last4(
            "Online Banking transfer from SAV 1234 Confirmation# 1"
        )
        == "1234"
    )
    assert extract_other_account_last4("VENMO DES:ACCTVERIFY ID:…") is None


def test_extract_confirmation_number_handles_real_descriptions() -> None:
    assert (
        extract_confirmation_number(
            "Online Banking transfer to CHK 7722 Confirmation# 5616819490"
        )
        == "5616819490"
    )
    assert extract_confirmation_number("INTERNATIONAL WH DES:SENDER ID:…") is None
