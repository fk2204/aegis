"""Pydantic models for the scoring/matching pipeline.

ScoreInput is the merged view of merchant + statement aggregates that the
scorer reads. ScoreResult is what comes out (recommendation + reasons).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.money import Money

Recommendation = Literal["approve", "decline", "refer"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class ScoreInput(_StrictModel):
    """Inputs to the scorer. Merged from merchants table + Aggregates."""

    merchant_id: UUID
    business_name: str
    owner_name: str
    state: str = Field(min_length=2, max_length=2, description="USPS state code")
    industry_naics: str | None = None
    time_in_business_months: int | None = Field(default=None, ge=0)
    credit_score: int | None = Field(default=None, ge=300, le=850)

    # Aggregates from the parser
    avg_daily_balance: Money
    true_revenue: Money
    num_nsf: int = Field(ge=0)
    days_negative: int = Field(ge=0)
    mca_positions: int = Field(ge=0)
    mca_daily_total: Money
    statement_period_start: date
    statement_period_end: date
    statement_days: int = Field(ge=0, description="length of the statement period in days")

    # Requested terms
    requested_amount: Money
    requested_factor: Decimal = Field(gt=Decimal("1"), le=Decimal("2"))
    requested_term_days: int = Field(ge=1, le=730)


class ScoreResult(_StrictModel):
    """Output of scoring. `recommendation` is the gate the rest of the system uses."""

    score: int = Field(ge=0, le=100)
    recommendation: Recommendation
    hard_decline_reasons: list[str] = Field(default_factory=list)
    soft_concerns: list[str] = Field(default_factory=list)
    estimated_payback_days: int | None = Field(default=None, ge=0)
    apr: Decimal | None = None


class FunderMatch(_StrictModel):
    """A funder candidate ranked against this merchant's profile."""

    funder_id: UUID
    funder_name: str
    match_score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    soft_concerns: list[str] = Field(default_factory=list)


class SubmissionPackage(_StrictModel):
    """Everything a funder needs to evaluate the deal."""

    id: UUID = Field(default_factory=uuid4)
    score_input: ScoreInput
    score_result: ScoreResult
    matched_funders: list[FunderMatch]
    email_subject: str
    email_body: str


__all__ = [
    "FunderMatch",
    "Recommendation",
    "ScoreInput",
    "ScoreResult",
    "SubmissionPackage",
]
