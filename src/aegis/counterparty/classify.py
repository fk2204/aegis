"""Top-level counterparty classifier.

Composes the three layers:

1. **Dictionary** (``patterns.py``) — fast deterministic regex against
   known shapes. Returns a class + reason + confidence, OR the
   ``_own_transfer_unresolved`` placeholder when the row is a
   transfer candidate that needs bundle matching.
2. **Bundle matcher** (``bundle_match.py``) — resolves the placeholder
   for transfer candidates into ``own_account`` (matched both sides)
   or ``own_account_unconfirmed`` (period gap OR no statement).
3. **LLM-assist** (deferred — interface stubbed below). For rows the
   dictionary doesn't match and the bundle matcher can't resolve,
   the operator can run a future LLM-assist pass. The classifier
   surfaces these as ``unknown`` with confidence 0 today; the operator
   reviews and grows the dictionary via the reclassify CLI.

THE LLM IS NOT CALLED FROM PARSE TIME IN THIS BUILD. The operator's
Q3 decision was "dictionary at parse time, LLM-assist only for
unknowns" — and "only for unknowns" today means "via the operator
review surface after the parse lands". The classifier here is fully
deterministic; the LLM layer is plumbed in a later commit when the
operator review surface exists. This keeps parse time cheap and
predictable.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from aegis.counterparty.bundle_match import (
    TransferMatch,
    match_own_account_transfers,
)
from aegis.counterparty.models import (
    BundleSummary,
    CounterpartyClass,
    CounterpartyClassification,
)
from aegis.counterparty.patterns import (
    _PLACEHOLDER_OWN_TRANSFER,
    extract_other_account_last4,
    lookup_dictionary,
)
from aegis.parser.models import ClassifiedTransaction


def _sign(amount: Decimal) -> int:
    if amount > 0:
        return 1
    if amount < 0:
        return -1
    return 0


def _resolve_transfer(
    txn: ClassifiedTransaction, match: TransferMatch
) -> tuple[CounterpartyClass, str, int, UUID | None]:
    """Map a TransferMatch outcome to a final
    (CounterpartyClass, reason, confidence, paired_id).

    Period-gap and no-statement both surface as
    own_account_unconfirmed but with distinct reason tokens so the
    operator follow-up list shows the right action.
    """
    if match.status == "matched":
        return "own_account", "matched_in_bundle", 100, match.paired_transaction_id
    if match.status == "period_gap":
        return (
            "own_account_unconfirmed",
            "missing_period_for_known_account",
            70,
            None,
        )
    if match.status == "no_statement":
        return (
            "own_account_unconfirmed",
            "no_statement_for_referenced_account",
            70,
            None,
        )
    # not_transfer — shouldn't reach here because the caller only calls
    # _resolve_transfer when the dictionary returned the placeholder.
    # Default safely to unknown.
    return "unknown", "transfer_placeholder_unresolved", 0, None


def classify_transaction(
    txn: ClassifiedTransaction,
    transfer_match: TransferMatch | None = None,
) -> CounterpartyClassification:
    """Classify ONE transaction. Used by tests and by the per-row CLI
    flow. The bundle-aware entry point is ``classify_bundle`` below.

    ``transfer_match`` should be supplied when the caller has already
    run bundle matching. When None (single-transaction classification
    with no bundle context), the row gets ``own_account_unconfirmed``
    if it looks like a transfer (we have no bundle to confirm
    pairing) — same surface-the-gap rule as
    ``no_statement_for_referenced_account``.
    """
    dict_hit = lookup_dictionary(txn.description, _sign(txn.amount))
    other_last4 = extract_other_account_last4(txn.description)

    if dict_hit is None:
        return CounterpartyClassification(
            transaction_id=txn.id,
            counterparty="unknown",
            confidence=0,
            reason="no_dictionary_match",
            other_account_last4=other_last4,
            paired_transaction_id=None,
        )

    raw_class, reason, confidence = dict_hit

    if raw_class == _PLACEHOLDER_OWN_TRANSFER:
        if transfer_match is None:
            # No bundle context — surface as unconfirmed by default.
            return CounterpartyClassification(
                transaction_id=txn.id,
                counterparty="own_account_unconfirmed",
                confidence=50,
                reason="no_bundle_context",
                other_account_last4=other_last4,
                paired_transaction_id=None,
            )
        cls, t_reason, t_conf, paired = _resolve_transfer(txn, transfer_match)
        return CounterpartyClassification(
            transaction_id=txn.id,
            counterparty=cls,
            confidence=t_conf,
            reason=t_reason,
            other_account_last4=other_last4,
            paired_transaction_id=paired,
        )

    # Direct dictionary hit (processor / card_paydown / international /
    # end_customer / unknown). ``raw_class`` is a string literal from
    # the dictionary; Pydantic validates it against the
    # ``CounterpartyClass`` Literal at construction time, so a bad
    # value in the dictionary fails loud at parse-time rather than
    # silent-passing through.
    cls_value: CounterpartyClass = raw_class  # type: ignore[assignment]
    return CounterpartyClassification(
        transaction_id=txn.id,
        counterparty=cls_value,
        confidence=confidence,
        reason=reason,
        other_account_last4=other_last4,
        paired_transaction_id=None,
    )


def classify_bundle(
    transactions_by_doc: dict[str, list[ClassifiedTransaction]],
    bundle_account_last4s: set[str] | None = None,
) -> tuple[dict[UUID, CounterpartyClassification], BundleSummary]:
    """Classify every transaction in a multi-statement parse bundle.

    Returns (per_transaction_map, bundle_summary). The summary is a
    rollup useful for operator audit ("of 144 VU txns, 33 were own_account
    transfers — 14 matched pairs, 20 unconfirmed; 3 international
    wires; 4 end-customer Zelle credits; …").

    ``bundle_account_last4s`` — if omitted, derived as best-effort from
    each transaction's "self" context. In normal use the caller passes
    the statement summaries' ``account_last4`` values explicitly so
    period-gap detection is correct.
    """
    flat = [t for txns in transactions_by_doc.values() for t in txns]
    if bundle_account_last4s is None:
        bundle_account_last4s = set()

    # Bundle matching only runs over transfer candidates. The matcher
    # itself sees every transaction but classifies non-transfer rows
    # as not_transfer; classify_transaction ignores those.
    matches = match_own_account_transfers(
        transactions_by_doc, bundle_account_last4s
    )

    out: dict[UUID, CounterpartyClassification] = {}
    matched_pairs_seen: set[frozenset[UUID]] = set()
    by_class: dict[CounterpartyClass, int] = {}
    unconfirmed_others: set[str] = set()

    for t in flat:
        m = matches.get(t.id)
        cc = classify_transaction(t, transfer_match=m)
        out[t.id] = cc
        by_class[cc.counterparty] = by_class.get(cc.counterparty, 0) + 1
        if cc.counterparty == "own_account" and cc.paired_transaction_id:
            matched_pairs_seen.add(
                frozenset({t.id, cc.paired_transaction_id})
            )
        if (
            cc.counterparty == "own_account_unconfirmed"
            and cc.other_account_last4
        ):
            unconfirmed_others.add(cc.other_account_last4)

    summary = BundleSummary(
        transaction_count=len(flat),
        by_class=by_class,
        unconfirmed_account_last4s=tuple(sorted(unconfirmed_others)),
        matched_pair_count=len(matched_pairs_seen),
    )
    return out, summary


__all__ = [
    "classify_bundle",
    "classify_transaction",
]
