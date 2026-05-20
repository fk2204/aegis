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


class MonthBreakdown(_StrictModel):
    """One calendar month's roll-up. Used for revenue trend + volatility scoring."""

    month: str = Field(pattern=r"^\d{4}-\d{2}$", description="ISO YYYY-MM")
    deposits: Money
    withdrawals: Money
    avg_balance: Money


class ScoreInput(_StrictModel):
    """Inputs to the scorer. Merged from multiple sources before scoring runs.

    Field groupings (each block lists its source):

    - Merchant identity (merchants table): `merchant_id`, `business_name`,
      `owner_name`, `state`, `industry_naics`, `industry_risk_tier`,
      `time_in_business_months`, `credit_score`. Hand-entered during
      onboarding; missing values become soft concerns rather than
      silent passes.

    - Parser aggregates (parser/aggregate.py): `avg_daily_balance`,
      `true_revenue`, `monthly_revenue` (true_revenue projected to 30
      days), `lowest_balance`, `num_nsf`, `days_negative`,
      `mca_positions`, `mca_daily_total`, `debt_to_revenue`,
      `payroll_detected`, `returned_ach_count`,
      `customer_concentration_pct`, `statement_period_*`,
      `statement_days`. All deterministic — no LLM influence.

    - Parser pipeline findings (parser/pipeline.py): `fraud_score`,
      `eof_markers`, `validation_passed`, `extraction_confidence`. Each
      maps to a hard-decline rule (eof > 1, validation failed) or a
      soft-scoring penalty.

    - Requested terms (operator-entered or broker-quoted):
      `requested_amount`, `requested_factor`, `requested_term_days`.
      The scorer may override these with `suggested_max_advance` /
      `recommended_factor_rate` based on tier.

    - Renewal context (optional, populated when this is a renewal):
      `is_renewal`, `prior_payoff_performance`, `prior_advance_count`.
      A `prior_payoff_performance == "default"` triggers a hard decline.

    - Multi-month context (optional, when sufficient history available):
      `monthly_breakdown`. Drives revenue-trend (3-month) and CV-based
      volatility (4-month) soft scoring.

    - DSCR inputs (optional, both required together):
      `total_monthly_obligations`, `proposed_daily_payment`. When both
      present, scoring runs DSCR hard decline (< 1.0) and adds a
      tier-based soft adjustment.
    """

    merchant_id: UUID
    business_name: str
    owner_name: str
    state: str = Field(min_length=2, max_length=2, description="USPS state code")
    industry_naics: str | None = None
    industry_risk_tier: Literal["low", "moderate", "elevated", "high", "avoid"] | None = None
    time_in_business_months: int | None = Field(default=None, ge=0)
    credit_score: int | None = Field(default=None, ge=300, le=850)

    # Aggregates from the parser
    avg_daily_balance: Money
    true_revenue: Money
    monthly_revenue: Money
    lowest_balance: Money
    num_nsf: int = Field(ge=0)
    days_negative: int = Field(ge=0)
    mca_positions: int = Field(ge=0)
    mca_daily_total: Money
    debt_to_revenue: Decimal = Field(ge=Decimal("0"))
    payroll_detected: bool = False
    returned_ach_count: int = Field(default=0, ge=0)
    customer_concentration_pct: int | None = Field(default=None, ge=0, le=100)
    statement_period_start: date
    statement_period_end: date
    statement_days: int = Field(ge=0, description="length of the statement period in days")

    # Parser pipeline findings
    fraud_score: int = Field(ge=0, le=100)
    eof_markers: int = Field(default=1, ge=0)
    validation_passed: bool = True
    extraction_confidence: int = Field(default=100, ge=0, le=100)

    # Requested terms
    requested_amount: Money
    requested_factor: Decimal = Field(gt=Decimal("1"), le=Decimal("2"))
    requested_term_days: int = Field(ge=1, le=730)

    # Renewal context (optional)
    is_renewal: bool = False
    prior_payoff_performance: Literal["early", "on_time", "late", "default"] | None = None
    prior_advance_count: int = Field(default=0, ge=0)

    # Multi-month context for trend / volatility scoring (optional)
    monthly_breakdown: list[MonthBreakdown] = Field(default_factory=list)

    # DSCR inputs (optional). When BOTH are present, scoring runs DSCR
    # hard decline (< 1.0) and soft scoring (1.5 / 1.15 / 1.0 thresholds).
    total_monthly_obligations: Money | None = None
    proposed_daily_payment: Money | None = None

    # Counterparty signals (master plan §5.7). Populated from
    # ``patterns.CounterpartySignals``. None when the underlying side
    # is empty (e.g. zero deposits in the window).
    top_counterparty_pct: int | None = Field(default=None, ge=0, le=100)
    top_counterparty_label: str | None = None
    top_5_revenue_share_pct: int | None = Field(default=None, ge=0, le=100)
    top_5_expense_share_pct: int | None = Field(default=None, ge=0, le=100)
    payroll_present: bool = False

    # Phase 9 hard-decline triggers (master plan §19 tasks 3).
    # ``acceleration_clause_triggered`` and ``unauthorized_withdrawal_dispute``
    # each promote to a hard-decline reason when True.
    # ``tampering_confirmed`` is the composite signal from the pipeline
    # (metadata + math + patterns); set True when fraud_score already
    # crossed the hard threshold via the multi-layer composite path,
    # so we can attach the named hard-decline reason in addition.
    acceleration_clause_triggered: bool = False
    unauthorized_withdrawal_dispute: bool = False
    tampering_confirmed: bool = False
    # 0..100 composite "looks AI-generated" score. Scored softly,
    # never auto-declined (per §6.4 — false positives kill real deals).
    ai_generated_score: int = Field(default=0, ge=0, le=100)


PaperGrade = Literal["A", "B", "C", "D"]


class ScoreResult(_StrictModel):
    """Output of scoring. `recommendation` is the gate the rest of the system uses."""

    score: int = Field(ge=0, le=100)
    tier: Literal["A", "B", "C", "D", "F"]
    recommendation: Recommendation
    hard_decline_reasons: list[str] = Field(default_factory=list)
    soft_concerns: list[str] = Field(default_factory=list)
    breakdown: list[dict[str, object]] = Field(default_factory=list)
    suggested_max_advance: Money = Decimal("0.00")
    recommended_factor_rate: Decimal = Decimal("0.00")
    recommended_holdback_pct: Decimal = Decimal("0.00")
    estimated_payback_days: int | None = Field(default=None, ge=0)
    apr: Decimal | None = None
    decline_details: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    """Structured detail attached to specific hard declines that need
    downstream audit + reporting. Currently populated for
    ``ofac_sanctions_match`` with key ``"ofac_matches"`` and entries shaped
    ``{"input_field": "business_name"|"owner_name", "matched_name": str,
    "sdn_uid": str}``. ``sdn_uid`` may be empty when the cached SDN feed
    pre-dates the uid plumbing or for hand-built test fixtures."""

    # Phase 9: industry-standard paper grade (master plan §5.8). Distinct
    # from ``tier`` (AEGIS internal score). Paper grade is the funder-
    # facing classification: A = first-position prime, B = mainstream,
    # C = sub-prime, D = last-resort. Hard-declined deals carry the
    # last paper grade they would have received before the decline
    # rules fired (defaults to D when a soft scoring path is not taken).
    paper_grade: PaperGrade = "D"
    paper_grade_reasons: list[str] = Field(default_factory=list)
    """Codes explaining which §5.8 criteria the deal satisfied / missed
    for each grade (e.g. ``"tib_24mo+"``, ``"adb_lt_8pct"``). Empty when
    paper_grade was not computed (hard decline path)."""


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


class DealMatchResult(_StrictModel):
    """Combined score + funder matches for a deal.

    Returned by ``POST /deals/score-with-matches``. Lighter than
    ``SubmissionPackage`` because the dashboard match panel doesn't need
    the email-template fields.
    """

    score: ScoreResult
    matched_funders: list[FunderMatch]


__all__ = [
    "DealMatchResult",
    "FunderMatch",
    "PaperGrade",
    "Recommendation",
    "ScoreInput",
    "ScoreResult",
    "SubmissionPackage",
]
