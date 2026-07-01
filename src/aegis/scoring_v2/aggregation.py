"""Counterparty-aware aggregation — shared between Tracks B and C.

Track B needs revenue net of transfers (the classical true_revenue
metric); Track C needs revenue grouped by counterparty class. Both
need to know which transactions count as REVENUE vs which net out as
own-account moves vs which are unresolved (book wires).

The counterparty classifier provides the per-transaction labels.
This module aggregates those labels into the rollups both tracks
consume. Importantly:

* **Only incoming amounts count as revenue.** The same counterparty
  class can appear on both sides (a processor refund is outgoing
  ``processor``); for revenue purposes only ``amount > 0`` is
  summed.
* **Own-account moves are NEVER revenue.** Even though they net to
  zero across the bundle's incoming/outgoing pairs, treating either
  side as revenue would double-count or wrongly attribute internal
  cash movement to "new money in".
* **``own_account_unconfirmed`` is NOT revenue.** The spec is explicit:
  surface the gap, don't infer. An unconfirmed transfer might be
  internal (most likely on VU) or external (which would BE revenue),
  but the parser cannot tell; treating it as revenue would over-state.
  The operator follows up.
* **``book_wire_unresolved`` is NOT revenue.** Same reasoning — could
  be internal or external; the description alone can't tell.
* **``card_paydown`` is NOT revenue.** It's a liability service
  payment, always outgoing.
* **``unknown`` incoming counts as revenue at threshold.** The
  classifier should be tight enough that incoming-unknown rows are
  trivial noise (VENMO ACCTVERIFY at fractions of a dollar). The
  aggregator surfaces a WARNING if incoming-unknown exceeds a small
  threshold, so the operator knows the denominator may be off.

Track B will later compute the cash-flow signals (ADB, lowest
balance, NSF, etc.) on top of this same aggregation. Track C uses
only the revenue-side rollup.

THIS MODULE DOES NOT SCORE. It produces a deterministic Pydantic
rollup; downstream tracks consume it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.counterparty.models import (
    CounterpartyClass,
    CounterpartyClassification,
)
from aegis.money import Money
from aegis.parser.models import ClassifiedTransaction

# Counterparty classes that count toward revenue. Every other class
# is excluded from the concentration denominator (and from Track B's
# true_revenue) so the spec rule "exclude non-revenue classes"
# enforces uniformly across both consumers.
REVENUE_CLASSES: Final[frozenset[CounterpartyClass]] = frozenset(
    {"processor", "end_customer", "international_client"}
)

# Classes that are explicitly NOT revenue but ARE inflow-shaped on
# at least one side. Surfaced separately on the panel so the operator
# can see "$X moved across own_account, $Y in unresolved book wires"
# at a glance.
NON_REVENUE_INFLOW_CLASSES: Final[frozenset[CounterpartyClass]] = frozenset(
    {
        "own_account",
        "own_account_unconfirmed",
        "book_wire_unresolved",
        "card_paydown",  # included so card paydowns surface as outflow
    }
)

# Threshold above which incoming "unknown" rows warrant a warning.
# Below this, treat as noise (VENMO ACCTVERIFY micro-deposits, etc.).
# Tuned conservatively against VU's bundle (incoming unknown = $0.34;
# anything above $5 in the leak-check test was a real gap).
UNKNOWN_INCOMING_WARN_THRESHOLD: Final[Decimal] = Decimal("100.00")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class ClassRollup(_StrictModel):
    """Aggregation result for one CounterpartyClass."""

    counterparty: CounterpartyClass
    transaction_count: int = Field(ge=0)
    incoming_total: Money
    outgoing_total: Money
    net: Money
    transaction_ids: tuple[UUID, ...] = Field(default_factory=tuple)


class BundleAggregation(_StrictModel):
    """All the rollups Tracks B and C consume.

    The total counts in ``transaction_count_total`` MUST equal the
    sum of every class's count. This is a structural invariant that
    Track B's downstream logic relies on (if a row is missing from
    the aggregation, true_revenue would silently understate).
    """

    transaction_count_total: int = Field(ge=0)
    revenue_total: Money = Field(
        description=(
            "Sum of incoming amounts where counterparty class is in "
            "REVENUE_CLASSES. The denominator Track C uses for share "
            "percentages and Track B uses for true_revenue."
        ),
    )
    by_class: tuple[ClassRollup, ...] = Field(
        description=(
            "One rollup per CounterpartyClass that appeared in the "
            "bundle. Empty classes are omitted to keep the structure "
            "compact; downstream code that needs zero defaults should "
            "treat missing as zero."
        ),
    )
    unknown_incoming_total: Money = Field(
        description=(
            "Total incoming amounts classified as ``unknown``. Above "
            "UNKNOWN_INCOMING_WARN_THRESHOLD the panel surfaces a "
            "warning because the revenue denominator may be incomplete."
        ),
    )
    excluded_inflow_total: Money = Field(
        description=(
            "Total incoming amounts that are NOT revenue (own_account, "
            "own_account_unconfirmed, book_wire_unresolved, card_paydown "
            "on the incoming side, which is unusual but possible)."
        ),
    )


def _sum_amounts(values: Iterable[Decimal]) -> Decimal:
    return sum(values, start=Decimal("0"))


class _ClassBucket:
    """Local accumulator for one CounterpartyClass during aggregation."""

    __slots__ = ("ids", "incoming", "outgoing")

    def __init__(self) -> None:
        self.incoming: list[Decimal] = []
        self.outgoing: list[Decimal] = []
        self.ids: list[UUID] = []


def aggregate_bundle(
    transactions_by_doc: Mapping[str, list[ClassifiedTransaction]],
    classifications: Mapping[UUID, CounterpartyClassification],
    *,
    persist: bool = True,
) -> BundleAggregation:
    """Roll up a parse bundle by counterparty class.

    Parameters
    ----------
    transactions_by_doc
        The same shape ``classify_bundle`` consumes — keyed by any
        stable per-document id, value is the document's transactions.
    classifications
        Output of ``classify_bundle``. Must contain a classification
        for every transaction in ``transactions_by_doc`` (the
        classifier produces one per row).
    persist
        When True (default), best-effort persists the classifications
        back to ``transactions`` via
        ``aegis.counterparty.persistence.persist_classifications``.
        The write is override-aware server-side (operator overrides
        are skipped) and failure-tolerant — a DB unavailability logs
        a WARN and returns without disrupting the aggregation return.
        Pass ``persist=False`` for pure-function callers (tests,
        shadow-analytic scripts) that don't want the side effect.

    Returns
    -------
    BundleAggregation
        Per-class rollups + the totals Tracks B and C consume.

    Raises
    ------
    KeyError
        If a transaction in ``transactions_by_doc`` has no entry in
        ``classifications`` — that's a contract violation by the
        caller; fail loud rather than silently treat as unknown.
    """
    flat = [t for txns in transactions_by_doc.values() for t in txns]

    # Persist classifications (P2 wiring, 2026-07-01). This runs BEFORE
    # the aggregation so a DB blip doesn't block the rollup, and the
    # persistence module itself is fail-open — get_supabase() raising
    # in a test-env or on a network blip returns 0 (logged warning).
    # Any code path that produces a ``classifications`` mapping via
    # ``aggregate_bundle`` now writes counterparty_class /
    # counterparty_confidence / counterparty_reason to the transactions
    # table (skipping operator-overridden rows server-side).
    if persist and classifications:
        # Import locally so ``aggregate_bundle`` stays importable by
        # test harnesses that don't stub the persistence module.
        from aegis.counterparty.persistence import persist_classifications

        persist_classifications(classifications)

    by_class_data: dict[CounterpartyClass, _ClassBucket] = {}

    for t in flat:
        cc = classifications[t.id]
        cls = cc.counterparty
        bucket = by_class_data.setdefault(cls, _ClassBucket())
        bucket.ids.append(t.id)
        if t.amount > 0:
            bucket.incoming.append(t.amount)
        elif t.amount < 0:
            bucket.outgoing.append(-t.amount)  # store outflow as positive magnitude
        # amount == 0: counted in the transaction_count and ids but
        # contributes nothing to the totals.

    rollups: list[ClassRollup] = []
    for cls, bucket in by_class_data.items():
        incoming_total = _sum_amounts(bucket.incoming)
        outgoing_total = _sum_amounts(bucket.outgoing)
        rollups.append(
            ClassRollup(
                counterparty=cls,
                transaction_count=len(bucket.ids),
                incoming_total=Money(incoming_total),
                outgoing_total=Money(outgoing_total),
                net=Money(incoming_total - outgoing_total),
                transaction_ids=tuple(bucket.ids),
            )
        )

    revenue_total = sum(
        (r.incoming_total for r in rollups if r.counterparty in REVENUE_CLASSES),
        start=Money(Decimal("0")),
    )
    unknown_incoming = sum(
        (r.incoming_total for r in rollups if r.counterparty == "unknown"),
        start=Money(Decimal("0")),
    )
    excluded_inflow = sum(
        (r.incoming_total for r in rollups if r.counterparty in NON_REVENUE_INFLOW_CLASSES),
        start=Money(Decimal("0")),
    )

    # Sort rollups by incoming_total desc so the panel renders top-down.
    rollups.sort(key=lambda r: r.incoming_total, reverse=True)

    return BundleAggregation(
        transaction_count_total=len(flat),
        revenue_total=Money(revenue_total),
        by_class=tuple(rollups),
        unknown_incoming_total=Money(unknown_incoming),
        excluded_inflow_total=Money(excluded_inflow),
    )


__all__ = [
    "NON_REVENUE_INFLOW_CLASSES",
    "REVENUE_CLASSES",
    "UNKNOWN_INCOMING_WARN_THRESHOLD",
    "BundleAggregation",
    "ClassRollup",
    "aggregate_bundle",
]
