"""Integration test: web-presence flags flow into ``match_funder.soft_concerns``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _make_score_input() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Inc.",
        owner_name="Jane Doe",
        state="MA",
        industry_naics="722511",
        industry_risk_tier="moderate",
        time_in_business_months=36,
        credit_score=680,
        avg_daily_balance=Decimal("5000.00"),
        true_revenue=Decimal("50000.00"),
        monthly_revenue=Decimal("50000.00"),
        lowest_balance=Decimal("1000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.20"),
        payroll_detected=True,
        returned_ach_count=0,
        customer_concentration_pct=25,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        fraud_score=10,
        eof_markers=1,
        validation_passed=True,
        extraction_confidence=95,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _make_score_result() -> ScoreResult:
    return ScoreResult(
        score=70,
        tier="B",
        recommendation="approve",
    )


def _make_funder() -> FunderRow:
    return FunderRow(
        id=uuid4(),
        name="Test Funder",
        active=True,
        operator_status="active",
        min_monthly_revenue=Decimal("20000"),
        min_credit_score=500,
        min_months_in_business=12,
        max_positions=5,
    )


def test_web_presence_flags_surface_as_soft_concerns() -> None:
    merchant = MerchantRow(
        id=uuid4(),
        business_name="Acme Inc.",
        state="MA",
        web_presence_flags=["bbb_unresolved_complaints", "active_lawsuits"],
    )
    match = match_funder(
        _make_funder(),
        _make_score_input(),
        _make_score_result(),
        merchant=merchant,
    )
    assert match is not None
    web_concerns = [c for c in match.soft_concerns if c.startswith("web presence:")]
    assert web_concerns == [
        "web presence: bbb_unresolved_complaints",
        "web presence: active_lawsuits",
    ]


def test_empty_flags_add_no_soft_concern() -> None:
    merchant = MerchantRow(
        id=uuid4(),
        business_name="Clean Co.",
        state="MA",
        web_presence_flags=[],
    )
    match = match_funder(
        _make_funder(),
        _make_score_input(),
        _make_score_result(),
        merchant=merchant,
    )
    assert match is not None
    assert not any(c.startswith("web presence:") for c in match.soft_concerns)


def test_match_funder_without_merchant_omits_web_presence_concerns() -> None:
    """Legacy callers that pass no ``merchant=`` get the same behaviour
    as pre-067 — no exception, no web-presence soft concerns."""
    match = match_funder(
        _make_funder(),
        _make_score_input(),
        _make_score_result(),
    )
    assert match is not None
    assert not any(c.startswith("web presence:") for c in match.soft_concerns)
