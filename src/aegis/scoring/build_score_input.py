"""Compose `ScoreInput` from merchant + parser-pipeline outputs.

This module is the merge layer between Supabase (merchants, analyses,
documents tables) and the scorer. It does NOT hit the network in this
phase — Phase 5 wires it to a live Supabase client. For now it accepts
a `merchant_row` dict + parser results and assembles `ScoreInput`.

Staleness rule
--------------
Statement period_end must be within `STALENESS_DAYS` of `as_of`. Older
statements raise `StaleStatementError` so scoring callers can refuse
rather than score against a frozen-in-time snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from aegis.parser.models import Aggregates
from aegis.scoring.models import ScoreInput

STALENESS_DAYS: Final[int] = 90


class StaleStatementError(RuntimeError):
    """Raised when the statement period ends > STALENESS_DAYS before `as_of`."""


@dataclass
class ParserSnapshot:
    """The parser-side facts the scorer needs.

    Phase 9 fields (all optional; default to None / False to keep
    existing callers working unchanged) carry counterparty + detector
    outputs from ``patterns.PatternAnalysis`` into the scorer.
    """

    aggregates: Aggregates
    fraud_score: int
    eof_markers: int
    validation_passed: bool
    extraction_confidence: int
    statement_period_start: date
    statement_period_end: date
    statement_days: int
    top_counterparty_pct: int | None = None
    top_counterparty_label: str | None = None
    top_5_revenue_share_pct: int | None = None
    top_5_expense_share_pct: int | None = None
    payroll_present: bool = False
    acceleration_clause_triggered: bool = False
    unauthorized_withdrawal_dispute: bool = False
    tampering_confirmed: bool = False
    ai_generated_score: int = 0


def build_score_input(
    merchant_row: dict[str, Any],
    parser: ParserSnapshot,
    *,
    requested_amount: Decimal,
    requested_factor: Decimal,
    requested_term_days: int,
    as_of: date,
) -> ScoreInput:
    """Assemble a ScoreInput. Raises `StaleStatementError` if the period is too old."""
    age = (as_of - parser.statement_period_end).days
    if age > STALENESS_DAYS:
        raise StaleStatementError(
            f"statement period_end={parser.statement_period_end} is {age} days "
            f"before as_of={as_of} (max {STALENESS_DAYS})"
        )

    monthly_revenue = _project_monthly(parser.aggregates.true_revenue.value, parser.statement_days)

    return ScoreInput(
        merchant_id=UUID(str(merchant_row["id"])),
        business_name=str(merchant_row["business_name"]),
        owner_name=str(merchant_row.get("owner_name", "")),
        state=str(merchant_row["state"]).upper(),
        industry_naics=merchant_row.get("industry_naics"),
        industry_risk_tier=merchant_row.get("industry_risk_tier"),
        time_in_business_months=merchant_row.get("time_in_business_months"),
        credit_score=merchant_row.get("credit_score"),
        avg_daily_balance=parser.aggregates.avg_daily_balance.value,
        true_revenue=parser.aggregates.true_revenue.value,
        monthly_revenue=monthly_revenue,
        lowest_balance=Decimal(str(merchant_row.get("lowest_balance", "0"))),
        num_nsf=parser.aggregates.num_nsf.value,
        days_negative=parser.aggregates.days_negative.value,
        mca_positions=int(merchant_row.get("mca_positions", 0)),
        mca_daily_total=parser.aggregates.mca_daily_total.value,
        debt_to_revenue=parser.aggregates.debt_to_revenue,
        payroll_detected=bool(merchant_row.get("payroll_detected", False)),
        returned_ach_count=int(merchant_row.get("returned_ach_count", 0)),
        customer_concentration_pct=merchant_row.get("customer_concentration_pct"),
        statement_period_start=parser.statement_period_start,
        statement_period_end=parser.statement_period_end,
        statement_days=parser.statement_days,
        fraud_score=parser.fraud_score,
        eof_markers=parser.eof_markers,
        validation_passed=parser.validation_passed,
        extraction_confidence=parser.extraction_confidence,
        requested_amount=requested_amount,
        requested_factor=requested_factor,
        requested_term_days=requested_term_days,
        is_renewal=bool(merchant_row.get("is_renewal", False)),
        prior_payoff_performance=merchant_row.get("prior_payoff_performance"),
        prior_advance_count=int(merchant_row.get("prior_advance_count", 0)),
        top_counterparty_pct=parser.top_counterparty_pct,
        top_counterparty_label=parser.top_counterparty_label,
        top_5_revenue_share_pct=parser.top_5_revenue_share_pct,
        top_5_expense_share_pct=parser.top_5_expense_share_pct,
        payroll_present=parser.payroll_present,
        acceleration_clause_triggered=parser.acceleration_clause_triggered,
        unauthorized_withdrawal_dispute=parser.unauthorized_withdrawal_dispute,
        tampering_confirmed=parser.tampering_confirmed,
        ai_generated_score=parser.ai_generated_score,
    )


def _project_monthly(period_revenue: Decimal, statement_days: int) -> Decimal:
    """Scale period revenue to a 30-day equivalent."""
    if statement_days <= 0:
        return Decimal("0.00")
    daily = period_revenue / Decimal(statement_days)
    return (daily * Decimal(30)).quantize(Decimal("0.01"))


__all__ = [
    "STALENESS_DAYS",
    "ParserSnapshot",
    "StaleStatementError",
    "build_score_input",
]
