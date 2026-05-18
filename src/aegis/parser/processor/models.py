"""Pydantic models for processor statements (Stripe / Square).

Mirrors the bank parser's strict-model convention: extras forbidden,
assignment validated, money is ``Money`` (Decimal). Never ``float`` —
the validation gate decides on $0.01 tolerances and a binary-float
representation would silently leak precision.

Five line-item kinds map directly to the validation identity:
    gross_charge - refund - chargeback - fee == payout (+/- $0.01).

A single processor statement carries:
- A ``ProcessorSummary`` block: the totals printed on the statement
  (Stripe's "Activity summary" / Square's "Sales summary"), plus the
  period and processor brand.
- A ``transactions`` list of ``ProcessorLineItem`` rows. Each row has
  ``source_page`` + ``source_line`` so the dossier drill-down points
  back at a specific printed row (same audit-trail discipline as
  bank statements).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aegis.money import Money, as_money

ProcessorLineKind = Literal[
    "gross_charge",  # money in from a customer card swipe / online charge
    "refund",        # money returned to the customer (operator-initiated)
    "chargeback",    # money clawed back via dispute
    "fee",           # processor's per-transaction or monthly fee
    "payout",        # deposit to the merchant's bank account
    "adjustment",    # rare: balance-only correction, not part of the math gate
]


class _StrictModel(BaseModel):
    """Base — extras forbidden, validate assignments, strip strings."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class ProcessorLineItem(_StrictModel):
    """One row from a processor statement line-item table.

    ``amount`` is always positive — the ``kind`` discriminates flow
    direction. Storing absolute values keeps the validation identity
    one operation: ``sum(gross) - sum(refund) - sum(chargeback) -
    sum(fee) == sum(payout)``. The bank-parser convention of signed
    amounts doesn't survive cleanly here because processor statements
    print refunds and chargebacks as positive numbers in a "deductions"
    column — easier to parse and validate as positive everywhere.
    """

    id: UUID = Field(default_factory=uuid4)
    posted_date: date
    description: str = Field(min_length=1)
    kind: ProcessorLineKind
    amount: Money
    source_page: int = Field(ge=1, description="1-indexed PDF page of this row")
    source_line: int = Field(ge=1, description="1-indexed line within the page")

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_money(cls, v: object) -> object:
        # Reject float at parse time so a JSON number cast to float is caught.
        if isinstance(v, float):
            return as_money(str(v))
        return v

    @field_validator("amount")
    @classmethod
    def _amount_nonneg(cls, v: Decimal) -> Decimal:
        # Sanity: the model encodes flow direction via ``kind``, so the
        # numeric value is always non-negative. A negative slip-through
        # would break the validation identity.
        if v < Decimal("0"):
            raise ValueError(f"processor line item amount must be non-negative; got {v}")
        return v


class ProcessorSummary(_StrictModel):
    """Printed activity summary block from the statement header.

    Stripe and Square each print these totals at the top of the
    document. The deterministic validator ties them out against the
    summed line items.
    """

    processor: Literal["stripe", "square"]
    business_name: str | None = None
    period_start: date
    period_end: date
    gross_volume: Money
    refunds_total: Money
    chargebacks_total: Money
    fees_total: Money
    payouts_total: Money
    transaction_count: int | None = Field(default=None, ge=0)


class ExtractedProcessorStatement(_StrictModel):
    """LLM pass-1 output: line items + printed summary. NO aggregates yet."""

    summary: ProcessorSummary
    transactions: list[ProcessorLineItem]


__all__ = [
    "ExtractedProcessorStatement",
    "ProcessorLineItem",
    "ProcessorLineKind",
    "ProcessorSummary",
]
