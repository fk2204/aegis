"""Counterparty classification — foundation for Tracks B and C of the
3-track scoring redesign (see ``docs/SCORING_REDESIGN_CONTINUATION.md``).

Classifies each transaction's COUNTERPARTY (who's on the other side of
the money flow) from its description. Orthogonal to the existing
``TransactionCategory`` axis in ``aegis.parser.models``: a single
``wire_in`` row could be from a processor, an international client, or
an end customer; a ``transfer`` could be an own-account move or a
card paydown. The counterparty class answers the "who?" question that
the category alone can't.

THIS MODULE IS PURE ADDITIVE CAPABILITY (2026-06-05). It does NOT
modify scoring, decline logic, or the parser's existing classification
pass. The classifier produces labels; downstream consumers (Tracks B
and C, the redesigned scoring layer) will read them later.

Strategy (per the operator's Q3 decision):

1. **Dictionary-first** — deterministic regex/pattern matching against a
   known catalog of processors, funder ACH prefixes, card networks,
   and bank-specific transfer description shapes (Bank of America's
   "Online Banking transfer to CHK XXXX", Chase's "REMOTE ONLINE
   TRANSFER", etc.). Covers the common case cheaply; no LLM cost.
2. **Bundle matching for own-account** — the load-bearing piece.
   "matched across the bundle" pairs a transfer-OUT on one statement
   to a transfer-IN on another by Confirmation# + opposite sign +
   equal magnitude. A successful pair = ``own_account`` (netted). A
   transfer to/from an account where we have a different statement
   but not the matching-period one = ``own_account_unconfirmed``
   (period gap; ask the merchant for the missing month). A transfer
   to an account we have NO statement for = also
   ``own_account_unconfirmed`` (no-statement gap; ask the merchant
   if the account is theirs). Both surface the gap rather than
   inferring ownership.
3. **LLM-assist for ``unknown``** — for descriptions the dictionary
   doesn't recognize, an LLM call labels them with the operator's
   chosen review surface (NOT autonomous — operator confirms before
   the label promotes into the dictionary).

The catalog grows via the ``scripts/reclassify_counterparties.py`` CLI
without re-parsing any PDFs.

See ``src/aegis/counterparty/models.py`` for the type, and
``tests/counterparty/`` for the real-data acceptance tests against
VU Development's CHK 7722 ↔ 7719 transfers (pairs) and CHK 9940
references (unpaired).
"""

from aegis.counterparty.classify import (
    classify_bundle,
    classify_transaction,
)
from aegis.counterparty.models import (
    BundleSummary,
    CounterpartyClass,
    CounterpartyClassification,
)

__all__ = [
    "BundleSummary",
    "CounterpartyClass",
    "CounterpartyClassification",
    "classify_bundle",
    "classify_transaction",
]
