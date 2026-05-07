"""Test the build_score_input merge layer + 90-day staleness gate."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.parser.models import Aggregates
from aegis.scoring.build_score_input import (
    STALENESS_DAYS,
    ParserSnapshot,
    StaleStatementError,
    build_score_input,
)


def _aggregates() -> Aggregates:
    """Minimal valid aggregates with empty source-id arrays."""
    return Aggregates(
        avg_daily_balance={"value": Decimal("12500.00"), "source_ids": []},
        true_revenue={"value": Decimal("110000.00"), "source_ids": []},
        num_nsf={"value": 0, "source_ids": []},
        days_negative={"value": 0, "source_ids": []},
        debt_to_revenue=Decimal("0.10"),
        mca_daily_total={"value": Decimal("50.00"), "source_ids": []},
    )


def _snapshot(period_end: date) -> ParserSnapshot:
    return ParserSnapshot(
        aggregates=_aggregates(),
        fraud_score=10,
        eof_markers=1,
        validation_passed=True,
        extraction_confidence=95,
        statement_period_start=period_end - timedelta(days=29),
        statement_period_end=period_end,
        statement_days=30,
    )


def _merchant_row() -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "business_name": "Acme Co",
        "owner_name": "Jane Doe",
        "state": "ca",
        "industry_naics": "238320",
        "time_in_business_months": 36,
        "credit_score": 700,
    }


def test_recent_statement_passes() -> None:
    today = date(2026, 5, 7)
    snap = _snapshot(period_end=today - timedelta(days=10))
    result = build_score_input(
        merchant_row=_merchant_row(),
        parser=snap,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        as_of=today,
    )
    assert result.state == "CA"
    assert result.business_name == "Acme Co"


def test_boundary_90_days_passes() -> None:
    today = date(2026, 5, 7)
    snap = _snapshot(period_end=today - timedelta(days=STALENESS_DAYS))
    result = build_score_input(
        merchant_row=_merchant_row(),
        parser=snap,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        as_of=today,
    )
    assert result is not None


def test_91_days_old_raises_stale_error() -> None:
    today = date(2026, 5, 7)
    snap = _snapshot(period_end=today - timedelta(days=STALENESS_DAYS + 1))
    with pytest.raises(StaleStatementError, match=r"max 90"):
        build_score_input(
            merchant_row=_merchant_row(),
            parser=snap,
            requested_amount=Decimal("50000.00"),
            requested_factor=Decimal("1.30"),
            requested_term_days=120,
            as_of=today,
        )


def test_state_uppercased() -> None:
    """Merchant row state should be normalized to uppercase USPS code."""
    today = date(2026, 5, 7)
    snap = _snapshot(period_end=today - timedelta(days=10))
    row = _merchant_row() | {"state": "ny"}
    result = build_score_input(
        merchant_row=row,
        parser=snap,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        as_of=today,
    )
    assert result.state == "NY"


def test_monthly_revenue_projected_to_30_days() -> None:
    """30/statement_days * true_revenue."""
    today = date(2026, 5, 7)
    snap = _snapshot(period_end=today - timedelta(days=10))
    snap.statement_days = 28
    result = build_score_input(
        merchant_row=_merchant_row(),
        parser=snap,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        as_of=today,
    )
    # true_revenue=110000 over 28 days -> 30/28 * 110000 ≈ 117857.14
    assert result.monthly_revenue.quantize(Decimal("0.01")) == Decimal("117857.14")
