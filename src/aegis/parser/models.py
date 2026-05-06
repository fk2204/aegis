"""Pydantic models for parser input/output.

Two-pass parser data flow:
  pass 1 -> ExtractedStatement (raw transactions + printed summary, no categories)
  validate gate -> ValidationResult
  pass 2 -> list[ClassifiedTransaction]
  aggregate -> Aggregates (with _source_ids back to transactions)

Every Transaction carries source_page + source_line so any aggregate can be
traced back to specific PDF rows. This is the auditability requirement.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aegis.money import Money, as_money

TransactionCategory = Literal[
    "deposit",
    "payroll",
    "ach_credit",
    "mca_debit",
    "nsf_fee",
    "wire_in",
    "wire_out",
    "transfer",
    "fee",
    "chargeback",
    "refund",
    "other",
]


class _StrictModel(BaseModel):
    """Base for parser models. Strict: forbid extras, validate assignments."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class Transaction(_StrictModel):
    """One row from a bank statement. Source-attributed."""

    id: UUID = Field(default_factory=uuid4)
    posted_date: date
    description: str = Field(min_length=1)
    amount: Money
    running_balance: Money | None = None
    source_page: int = Field(ge=1, description="1-indexed PDF page of this row")
    source_line: int = Field(ge=1, description="1-indexed line within the page")

    @field_validator("amount", "running_balance", mode="before")
    @classmethod
    def _coerce_money(cls, v: object) -> object:
        # Reject float at parse time so JSON numbers coerced to float are caught.
        if isinstance(v, float):
            return as_money(str(v))
        return v


class StatementSummary(_StrictModel):
    """The printed totals from the statement header/footer.

    These are quoted by Claude in pass 1 from the printed PDF text. They
    are the ground-truth that the validation gate ties out against.
    """

    beginning_balance: Money
    ending_balance: Money
    deposit_total: Money
    withdrawal_total: Money
    period_start: date
    period_end: date
    printed_transaction_count: int | None = Field(default=None, ge=0)


class ExtractedStatement(_StrictModel):
    """Pass-1 output: raw transactions + printed summary. NO classification."""

    summary: StatementSummary
    transactions: list[Transaction]


class ClassifiedTransaction(Transaction):
    """Pass-2 output: a Transaction plus a category and confidence."""

    category: TransactionCategory
    classification_confidence: int = Field(
        ge=0,
        le=100,
        description="0-100; rows below the configured threshold trigger review",
    )


# A money-or-int aggregate paired with its source transaction ids.
# Used for every metric in Aggregates so the audit drill-down works.
class _SourcedMoney(_StrictModel):
    value: Money
    source_ids: list[UUID] = Field(default_factory=list)


class _SourcedInt(_StrictModel):
    value: int = Field(ge=0)
    source_ids: list[UUID] = Field(default_factory=list)


class Aggregates(_StrictModel):
    """Deterministic aggregates derived from classified transactions.

    Every metric carries the list of transaction ids that produced it, so
    the merchant detail page can answer "where did this number come from?"
    """

    avg_daily_balance: _SourcedMoney
    true_revenue: _SourcedMoney
    num_nsf: _SourcedInt
    days_negative: _SourcedInt
    debt_to_revenue: Money
    mca_daily_total: _SourcedMoney


class ValidationResult(_StrictModel):
    """Output of the deterministic validation gate (parse pass 1 -> pass 2).

    `passed=False` means the document goes to manual_review with no retry.
    """

    passed: bool
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# Re-exports for convenience in downstream modules.
__all__ = [
    "Aggregates",
    "ClassifiedTransaction",
    "ExtractedStatement",
    "StatementSummary",
    "Transaction",
    "TransactionCategory",
    "ValidationResult",
]
