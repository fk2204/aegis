"""Scoring test fixtures.

Provides a `clean_deal` ScoreInput that should pass all hard-decline gates
(no OFAC, no stacking, no fraud, etc.) so individual tests can mutate one
field at a time to assert that hard-decline rules fire correctly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.scoring.models import ScoreInput


@pytest.fixture
def clean_deal() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="238320",
        industry_risk_tier="moderate",
        time_in_business_months=48,
        credit_score=720,
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
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
