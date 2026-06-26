"""Dossier-shape Stripe aggregates.

``ProcessorAggregates`` (in ``aggregate.py``) is the validator-shape
view of a processor statement — the totals that have to tie out
against the identity ``gross - refund - chargeback - fee == payout``.
The dossier needs a SUPERSET of those numbers so the operator can
underwrite a card-heavy merchant at a glance:

  * gross volume, total fees, total net (kept from ``ProcessorAggregates``)
  * AVG DAILY VOLUME — gross / period_days (new; capacity sizing)
  * PAYOUT COUNT — wires up Stripe payout cadence question
  * CHARGEBACK COUNT, REFUND COUNT, CHARGE COUNT (already on
    ``ProcessorAggregates``, surfaced here for the dossier section)
  * REFUND RATE — refund_count / max(1, charge_count)
  * PERIOD_START / PERIOD_END / PERIOD_DAYS

Every aggregate keeps its ``_source_ids`` reference to the underlying
``ProcessorLineItem.id`` set. The drill-down works the same way as
the bank parser's aggregate drill-downs.

This module is PURE: takes a validated extraction + the validator's
aggregates, returns a new structure. No I/O, no LLM, no DB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money
from aegis.parser.processor.aggregate import ProcessorAggregates
from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
)

# ``parse_method`` discriminates CSV vs. PDF/vision input on the
# dossier so the operator can tell at a glance which path produced
# the numbers. CSV is deterministic; PDF goes through Bedrock vision.
ParseMethod = Literal["csv", "pdf_vision"]


class _SourcedMoney(BaseModel):
    """A Money value paired with the row IDs that produced it.

    Same shape as ``aggregate._SourcedMoney`` — re-declared here so
    this module doesn't reach into the sibling's private name.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    value: Money
    source_ids: list[UUID] = Field(default_factory=list)


class _SourcedInt(BaseModel):
    """An int count paired with the row IDs that produced it."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    value: Annotated[int, Field(ge=0)]
    source_ids: list[UUID] = Field(default_factory=list)


class StripeDossierAggregates(BaseModel):
    """Stripe metrics surfaced on the merchant dossier.

    Each money field carries ``_source_ids`` so the dossier drill-down
    can resolve "where did this number come from?" to a specific set
    of line items. Identical discipline to ``ProcessorAggregates`` and
    the bank parser's aggregates.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    total_gross_volume: _SourcedMoney
    total_fees: _SourcedMoney
    total_net_volume: _SourcedMoney
    total_payouts: _SourcedMoney

    payout_count: int = Field(ge=0)
    avg_daily_volume: Money

    period_start: date
    period_end: date
    period_days: int = Field(ge=1)

    chargeback_count: int = Field(ge=0)
    refund_count: int = Field(ge=0)
    charge_count: int = Field(ge=0)
    refund_rate: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        description="refund_count / max(1, charge_count); 0 when no charges",
    )


class StripeParseResult(BaseModel):
    """End-to-end Stripe parse output for the dossier surface.

    Carries the validated extraction (raw line items + printed
    summary), the dossier-shape aggregates, and a ``parse_method``
    discriminator that surfaces "CSV vs. PDF vision" on the dossier
    so the operator can tell at a glance which path produced the
    numbers.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )

    extraction: ExtractedProcessorStatement
    aggregates: StripeDossierAggregates
    parse_method: ParseMethod
    period_days: int = Field(ge=1)


def build_stripe_dossier_aggregates(
    extraction: ExtractedProcessorStatement,
    base_aggregates: ProcessorAggregates,
) -> StripeDossierAggregates:
    """Derive the dossier-shape aggregates from a validated extraction.

    ``base_aggregates`` is the validator-shape view (``ProcessorAggregates``);
    we reuse its ``_SourcedMoney`` shape for gross / fees / payouts and
    derive the additional dossier metrics on top.

    Period denominator gotcha
    -------------------------
    ``period_days = (period_end - period_start).days + 1`` — inclusive
    of both endpoints. A single-day CSV (one row, one date) yields
    ``period_days = 1``, not 0, so ``avg_daily_volume`` never divides
    by zero. Single-day exports are a real shape (operator exports
    one day's activity); they shouldn't crash the dossier.
    """
    period_start = extraction.summary.period_start
    period_end = extraction.summary.period_end
    if period_end < period_start:
        # Defensive — the validator gate normally catches this. If we
        # reach here, snap to a single-day window so the dossier renders
        # at least one usable number and the operator sees the issue on
        # the period chip.
        period_days = 1
    else:
        period_days = (period_end - period_start).days + 1
    if period_days <= 0:
        period_days = 1

    rows: list[ProcessorLineItem] = list(extraction.transactions)

    # net_volume = gross - refund - chargeback - fee. The validator
    # gate has already confirmed this equals payouts (+/- $0.01); we
    # recompute here to attach our own _source_ids set covering every
    # contributing row (so the drill-down on net surfaces gross +
    # refund + chargeback + fee rows).
    gross_value = base_aggregates.gross_volume.value
    fees_value = base_aggregates.fees_total.value
    refund_value = base_aggregates.refunds_total.value
    chargeback_value = base_aggregates.chargebacks_total.value
    payouts_value = base_aggregates.payouts_total.value

    net_value = gross_value - refund_value - chargeback_value - fees_value
    net_ids: list[UUID] = (
        list(base_aggregates.gross_volume.source_ids)
        + list(base_aggregates.refunds_total.source_ids)
        + list(base_aggregates.chargebacks_total.source_ids)
        + list(base_aggregates.fees_total.source_ids)
    )

    avg_daily_volume = (gross_value / Decimal(period_days)).quantize(Decimal("0.01"))

    payout_rows = [r for r in rows if r.kind == "payout"]
    charge_count = len(base_aggregates.gross_volume.source_ids)
    refund_count = len(base_aggregates.refunds_total.source_ids)
    chargeback_count = len(base_aggregates.chargebacks_total.source_ids)

    refund_rate = (
        Decimal(refund_count) / Decimal(charge_count) if charge_count > 0 else Decimal("0")
    )

    return StripeDossierAggregates(
        total_gross_volume=_SourcedMoney(
            value=gross_value,
            source_ids=list(base_aggregates.gross_volume.source_ids),
        ),
        total_fees=_SourcedMoney(
            value=fees_value,
            source_ids=list(base_aggregates.fees_total.source_ids),
        ),
        total_net_volume=_SourcedMoney(
            value=net_value,
            source_ids=net_ids,
        ),
        total_payouts=_SourcedMoney(
            value=payouts_value,
            source_ids=list(base_aggregates.payouts_total.source_ids),
        ),
        payout_count=len(payout_rows),
        avg_daily_volume=avg_daily_volume,
        period_start=period_start,
        period_end=period_end,
        period_days=period_days,
        chargeback_count=chargeback_count,
        refund_count=refund_count,
        charge_count=charge_count,
        refund_rate=refund_rate,
    )


__all__ = [
    "ParseMethod",
    "StripeDossierAggregates",
    "StripeParseResult",
    "build_stripe_dossier_aggregates",
]
