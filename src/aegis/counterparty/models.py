"""Pydantic types for counterparty classification.

Orthogonal to ``aegis.parser.models.TransactionCategory``: counterparty
answers "who's on the other side?", category answers "what kind of
movement?". A row can be both ``(category="wire_in",
counterparty="international_client")`` or ``(category="transfer",
counterparty="own_account")`` etc.

The classification carries enough provenance that the operator (and
the future scoring tracks) can drill into *why* a row was labeled the
way it was:

- ``confidence`` = 0..100, where dictionary hits land at 95-100,
  pattern-only matches at 70-90, and LLM-assist at the model's
  reported confidence (capped at 60 for un-reviewed labels).
- ``reason`` = short token identifying the rule that fired
  (``"boa_chk_transfer"``, ``"woocommerce"``, ``"zelle_incoming"``,
  ``"llm_assist"``, etc.). Enables ``GROUP BY reason`` analysis on
  large parses.
- ``other_account_last4`` = parsed last-4 from the description when
  the row references one (e.g. ``"CHK 7722"`` → ``"7722"``,
  ``"CRD 0993"`` → ``"0993"``). Used by the bundle matcher.
- ``paired_transaction_id`` = for matched ``own_account`` rows, the
  transaction id on the OTHER side of the pair. Lets a future audit
  view render "this $125,792.37 from CHK 7719 on 2026-03-15 nets
  against this $-125,792.37 to CHK 7722 on the same day". Null for
  un-paired rows.

The classifier never mutates the underlying ``ClassifiedTransaction``
— it produces a separate ``CounterpartyClassification`` keyed by
transaction id. This keeps the existing parser-pipeline outputs
unchanged (additive guarantee) while making the new labels easy to
persist later as their own column / table.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CounterpartyClass = Literal[
    # Payment rails / sales channels — revenue. Low concentration weight
    # because a processor aggregates many end customers.
    "processor",
    # Transfer between two accounts the merchant controls, with BOTH
    # sides present in the parse bundle and pairable via Confirmation#
    # / amount / date. Netted out of revenue; never counted as risk.
    "own_account",
    # Transfer references an account the merchant might control but the
    # other side isn't in our bundle — either we lack the statement for
    # that account entirely (CHK 9940 in VU's case) or we have it but
    # not for the matching period (CHK 7722 references on 7719's
    # non-March statements). Surface the gap. Operator follows up.
    "own_account_unconfirmed",
    # Payment to a credit-card account (e.g. "payment to CRD 0993").
    # Expense, informational. Distinct from an own_account transfer
    # because a credit card is a liability account, not a deposit
    # account.
    "card_paydown",
    # Incoming international wire — revenue, but a durability question
    # rather than a fraud signal. International concentration affects
    # repayment risk modeling, not statement integrity.
    "international_client",
    # Named end customer (a specific business or individual via
    # Zelle/wire/ACH). This IS the concentration signal — if 60% of
    # revenue is from one named end customer, that's a genuine
    # repayment-risk concern.
    "end_customer",
    # Large BoA book wire ("WIRE TYPE:BOOK IN/OUT") that carries only
    # a TRN: tracking number and no CHK/SAV last-4 reference — the
    # description can't tell us whether it was an internal own-account
    # move (BoA→BoA between accounts the merchant controls) or an
    # external wire (revenue from a counterparty whose identity is
    # lost in the TRN). The bundle matcher cannot pair these (no CHK
    # reference, no Confirmation#). Held in this resolvable state
    # until a human determines which it is. **NOT counted as revenue,
    # NOT netted as own_account.** Track B and Track C must treat this
    # bucket as a separate aggregation cell — misrouting it would
    # distort both true_revenue and concentration math (verified
    # 2026-06-05 against VU Development: 15 BOOK rows totalling
    # ~$1.97M would have hidden in `unknown` without this class).
    "book_wire_unresolved",
    # Default surface for review. Used when no dictionary rule matches
    # and the LLM-assist hasn't yet labeled the row. Operator review
    # promotes unknown rows into the dictionary over time.
    "unknown",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class CounterpartyClassification(_StrictModel):
    """One transaction's counterparty label + provenance.

    Keyed by ``transaction_id`` so downstream callers can join back to
    the underlying ``ClassifiedTransaction`` without mutating it.
    """

    transaction_id: UUID
    counterparty: CounterpartyClass
    confidence: int = Field(ge=0, le=100)
    reason: str = Field(default="", max_length=64)
    other_account_last4: str | None = Field(
        default=None, max_length=8,
        description=(
            "Parsed last-4 (or last-N) of the OTHER account this row "
            "references — e.g. 'CHK 7722' → '7722', 'CRD 0993' → '0993'. "
            "Null for rows that don't reference an account number."
        ),
    )
    paired_transaction_id: UUID | None = Field(
        default=None,
        description=(
            "For counterparty='own_account', the transaction id on the "
            "OTHER side of the pair (same Confirmation#, opposite sign, "
            "equal magnitude, in the same bundle). Null otherwise."
        ),
    )


class BundleSummary(_StrictModel):
    """Bundle-level rollup produced alongside the per-transaction labels.

    Lets callers see at a glance how a multi-statement parse classified
    by counterparty class. The percentages here aren't used for scoring
    in this build — they're a debugging / audit surface for the
    operator while the dictionary grows.
    """

    transaction_count: int = Field(ge=0)
    by_class: dict[CounterpartyClass, int] = Field(default_factory=dict)
    unconfirmed_account_last4s: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Unique last-4s that surfaced as own_account_unconfirmed — "
            "the operator follow-up list. Sorted for stable rendering."
        ),
    )
    matched_pair_count: int = Field(
        default=0, ge=0,
        description=(
            "Number of own_account pairs the bundle matcher confirmed "
            "(each pair counts ONCE here, even though it produces two "
            "own_account classifications)."
        ),
    )


__all__ = [
    "BundleSummary",
    "CounterpartyClass",
    "CounterpartyClassification",
]
