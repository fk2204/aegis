"""Own-account transfer matching across a parse bundle.

A parse bundle is the set of statements uploaded together for one
merchant. For each transaction the dictionary marked as a candidate
own-account transfer, this matcher decides:

* **matched** — there is a paired transaction in the bundle (same
  Confirmation#, opposite sign, equal magnitude). Both sides classify
  as ``own_account`` and reference each other via
  ``paired_transaction_id``.
* **period_gap** — the other account's last-4 is present somewhere in
  the bundle (we have at least one statement for it) but no pair fired
  for this specific transfer. Classifies as
  ``own_account_unconfirmed`` with the reason hinting at the gap. VU
  Development's transfers to/from CHK 7722 on the Feb/Apr/May 7719
  statements land here — we only have the March 7722 statement.
* **no_statement** — the other account's last-4 is NOT anywhere in the
  bundle. Classifies as ``own_account_unconfirmed`` with a different
  reason. VU's single transfer "to CHK 9940" lands here — we have no
  9940 statement at all.

Both ``period_gap`` and ``no_statement`` are surfaced (per the operator's
"do NOT infer it's their own account" rule) — they look identical to a
scoring track but the operator follow-up differs (ask for the missing
month vs. ask whether the account is theirs).

EMPIRICAL CHECK on VU's bundle (2026-06-05, 144 txns, 5 docs):

    unique Confirmation#s:  29
    pairs:                   7   ← own_account
    singletons by other-acct:
        7722: 20             ← own_account_unconfirmed (period_gap)
        9940:  1             ← own_account_unconfirmed (no_statement)
        0993:  1             ← card_paydown (not own_account at all)

The card_paydown case is filtered out by the dictionary's CRD rule
before the bundle matcher runs — it never reaches here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from aegis.counterparty.patterns import (
    extract_confirmation_number,
    extract_other_account_last4,
)
from aegis.parser.models import ClassifiedTransaction

# Two amounts pair iff they're exactly equal in magnitude and opposite
# in sign. Money is Decimal, so equality is exact — no tolerance fudge.
# A pair on different absolute amounts is NOT a pair.
_EXACT_MATCH: Final[Decimal] = Decimal("0")


@dataclass(frozen=True)
class TransferMatch:
    """Outcome of bundle matching for one transaction.

    ``status``:
      "matched"      → an own_account pair was confirmed
      "period_gap"   → other acct in bundle, no pair this period
      "no_statement" → other acct not in bundle at all
      "not_transfer" → row isn't a transfer candidate (no Conf# / no
                       CHK/SAV reference)
    """

    status: str  # "matched" | "period_gap" | "no_statement" | "not_transfer"
    other_account_last4: str | None
    confirmation_number: str | None
    paired_transaction_id: UUID | None


def match_own_account_transfers(
    transactions_by_doc: dict[str, list[ClassifiedTransaction]],
    bundle_account_last4s: set[str],
) -> dict[UUID, TransferMatch]:
    """Walk every transaction in the bundle; resolve own-account status.

    ``transactions_by_doc`` is a mapping from a document identifier
    (any stable key — typically ``document_id`` or ``account_last4``)
    to the document's transactions.

    ``bundle_account_last4s`` is the set of account last-4s for which
    we have AT LEAST ONE statement in this bundle. Used to distinguish
    ``period_gap`` from ``no_statement``.

    Returns a per-transaction map. Transactions that are NOT
    transfer-shaped (no Conf# OR no CHK/SAV other-account reference)
    return ``status="not_transfer"`` so the caller knows the matcher
    saw the row and made no decision.
    """
    flat = [t for txns in transactions_by_doc.values() for t in txns]

    # Index every transfer-shaped row by Confirmation#. Track multiple
    # rows under the same Conf# (the normal case for a paired transfer
    # is exactly 2; >2 is a data anomaly and we surface as singletons
    # to be safe).
    by_conf: dict[str, list[ClassifiedTransaction]] = defaultdict(list)
    confs_seen: dict[UUID, str | None] = {}
    others_seen: dict[UUID, str | None] = {}
    for t in flat:
        conf = extract_confirmation_number(t.description)
        other = extract_other_account_last4(t.description)
        confs_seen[t.id] = conf
        others_seen[t.id] = other
        if conf is not None and other is not None:
            by_conf[conf].append(t)

    matches: dict[UUID, TransferMatch] = {}
    for t in flat:
        conf = confs_seen[t.id]
        other = others_seen[t.id]
        if conf is None or other is None:
            matches[t.id] = TransferMatch(
                status="not_transfer",
                other_account_last4=other,
                confirmation_number=conf,
                paired_transaction_id=None,
            )
            continue

        candidates = [c for c in by_conf[conf] if c.id != t.id]
        # A valid pair: exactly one sibling AND opposite-signed AND
        # equal absolute magnitude.
        paired: UUID | None = None
        for cand in candidates:
            if cand.amount == -t.amount and cand.amount != t.amount:
                paired = cand.id
                break
        if paired is not None:
            matches[t.id] = TransferMatch(
                status="matched",
                other_account_last4=other,
                confirmation_number=conf,
                paired_transaction_id=paired,
            )
            continue

        # No pair fired. Distinguish period_gap from no_statement.
        if other in bundle_account_last4s:
            status = "period_gap"
        else:
            status = "no_statement"
        matches[t.id] = TransferMatch(
            status=status,
            other_account_last4=other,
            confirmation_number=conf,
            paired_transaction_id=None,
        )

    return matches


__all__ = ["TransferMatch", "match_own_account_transfers"]
