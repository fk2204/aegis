"""Deterministic processor-statement aggregates.

Pure-Python computation over the validated line-item rows. Every
aggregate carries the list of source row UUIDs that produced it —
same ``_source_ids`` discipline as the bank parser's aggregates so the
audit drill-down ("where did this number come from?") works.

Run AFTER the validation gate has passed. Aggregates over un-validated
rows would mix invented and real data; the gate is the firewall.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money
from aegis.parser.processor.models import ProcessorLineItem


class _SourcedMoney(BaseModel):
    """A Money value paired with the row IDs that produced it."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    value: Money
    source_ids: list[UUID] = Field(default_factory=list)


class _SourcedInt(BaseModel):
    """An int count paired with the row IDs that produced it."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    value: Annotated[int, Field(ge=0)]
    source_ids: list[UUID] = Field(default_factory=list)


class ProcessorAggregates(BaseModel):
    """Final processor metrics surfaced to scoring + the dossier.

    ``net_revenue`` is the operator-facing "what did this merchant
    actually keep" number: gross - refunds - chargebacks - fees, which
    by the validation identity equals payouts (within $0.01).
    Storing it separately from payouts lets downstream code reason
    about the merchant's revenue stream without re-deriving the
    identity.

    ``chargeback_ratio`` is the chargeback-to-gross ratio expressed as
    a Decimal (NOT a float — keeping it Decimal lets scoring run
    risk-threshold checks without re-coercion). Zero when there are
    no charges in the period.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    gross_volume: _SourcedMoney
    refunds_total: _SourcedMoney
    chargebacks_total: _SourcedMoney
    fees_total: _SourcedMoney
    payouts_total: _SourcedMoney
    net_revenue: _SourcedMoney

    transaction_count: _SourcedInt
    refund_count: _SourcedInt
    chargeback_count: _SourcedInt

    chargeback_ratio: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description="chargebacks_total / gross_volume; 0 when gross_volume == 0",
    )


def aggregate_processor(rows: list[ProcessorLineItem]) -> ProcessorAggregates:
    """Build the aggregates from validated line items.

    The grouping is by ``kind``:
        - gross_charge → gross_volume + transaction_count
        - refund       → refunds_total + refund_count
        - chargeback   → chargebacks_total + chargeback_count
        - fee          → fees_total
        - payout       → payouts_total
        - adjustment   → IGNORED here (the validator's identity excludes
          adjustments). Keeping adjustments out of the aggregates means
          a small balance correction doesn't poison gross_volume.
    """
    gross_value = Decimal("0.00")
    gross_ids: list[UUID] = []
    refund_value = Decimal("0.00")
    refund_ids: list[UUID] = []
    cb_value = Decimal("0.00")
    cb_ids: list[UUID] = []
    fee_value = Decimal("0.00")
    fee_ids: list[UUID] = []
    payout_value = Decimal("0.00")
    payout_ids: list[UUID] = []

    for row in rows:
        if row.kind == "gross_charge":
            gross_value += row.amount
            gross_ids.append(row.id)
        elif row.kind == "refund":
            refund_value += row.amount
            refund_ids.append(row.id)
        elif row.kind == "chargeback":
            cb_value += row.amount
            cb_ids.append(row.id)
        elif row.kind == "fee":
            fee_value += row.amount
            fee_ids.append(row.id)
        elif row.kind == "payout":
            payout_value += row.amount
            payout_ids.append(row.id)
        # "adjustment" deliberately skipped — see docstring.

    net_revenue_value = gross_value - refund_value - cb_value - fee_value
    # net_revenue's source_ids are the union of every kind that
    # contributed to its derivation. The dossier "drill-down on
    # net_revenue" link should surface every row that mattered.
    net_ids: list[UUID] = list(gross_ids) + list(refund_ids) + list(cb_ids) + list(fee_ids)

    chargeback_ratio = (
        cb_value / gross_value if gross_value > Decimal("0") else Decimal("0")
    )

    return ProcessorAggregates(
        gross_volume=_SourcedMoney(value=gross_value, source_ids=gross_ids),
        refunds_total=_SourcedMoney(value=refund_value, source_ids=refund_ids),
        chargebacks_total=_SourcedMoney(value=cb_value, source_ids=cb_ids),
        fees_total=_SourcedMoney(value=fee_value, source_ids=fee_ids),
        payouts_total=_SourcedMoney(value=payout_value, source_ids=payout_ids),
        net_revenue=_SourcedMoney(value=net_revenue_value, source_ids=net_ids),
        transaction_count=_SourcedInt(value=len(gross_ids), source_ids=gross_ids),
        refund_count=_SourcedInt(value=len(refund_ids), source_ids=refund_ids),
        chargeback_count=_SourcedInt(value=len(cb_ids), source_ids=cb_ids),
        chargeback_ratio=chargeback_ratio,
    )


__all__ = [
    "ProcessorAggregates",
    "aggregate_processor",
]
