"""Shared lender-label heuristic for MCA debit grouping + display.

Single source of truth for the "extract a funder name from an ACH /
debit descriptor" heuristic used by:

* ``aegis.web._stacking_card`` (display label for the existing
  per-debit stacking card)
* ``aegis.scoring_v2.mca_stack`` (group-by key for the new MCA-stack
  aggregation chips)

Lives in ``scoring_v2`` rather than ``web`` because the scoring layer
must not pull in ``aegis.web`` (importing ``aegis.web.__init__``
triggers the full router stack, which fires the
``AEGIS_DATA_RESIDENCY_CONFIRMED`` boot guard — fine in tests with
the env var set, but a layering violation regardless and a
hard-import-error at module load time without the env var).

The heuristic itself is the same one the stacking card has shipped
since day one: take up to the first three alphanumeric tokens from
the descriptor, dropping a small set of bank-noise tokens
(``DEBIT`` / ``ACH`` / ``PMT`` / ``LLC`` / etc.). Funder names show
up at the front of the merchant descriptor
(``KAPITUS DEBIT 12345``, ``ON DECK ACH PMT``); 3 tokens is enough
for a readable label without dragging in transaction sequence
numbers downstream of ``DEBIT``.
"""

from __future__ import annotations

from typing import Final

_NOISE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "DEBIT",
        "ACH",
        "PMT",
        "PAYMENT",
        "WITHDRAWAL",
        "DAILY",
        "TRANSFER",
        "INC",
        "LLC",
        "LTD",
        "CO",
    }
)


def lender_label(description: str) -> str:
    """Extract a short funder label from a debit descriptor.

    Up to the first 3 alphanumeric tokens, skipping noise tokens.
    Returns ``"(unknown)"`` for empty input. Falls back to the first
    raw token if all candidates were noise.
    """
    cleaned = description.strip()
    if not cleaned:
        return "(unknown)"
    tokens = [tok for tok in cleaned.split() if tok.isalnum()]
    if not tokens:
        return cleaned[:32]
    keep: list[str] = []
    for tok in tokens[:5]:
        if tok.upper() in _NOISE_TOKENS:
            continue
        keep.append(tok)
        if len(keep) == 3:
            break
    return " ".join(keep) if keep else tokens[0][:32]


__all__ = ["lender_label"]
