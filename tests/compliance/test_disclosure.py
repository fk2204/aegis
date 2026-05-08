"""Disclosure router + unaudited warning hook tests.

Owns the remaining Phase 4 unit tests:
  (4) Tier 3 state → render_disclosure raises StateNotAudited
  (6) Tier 3 state → warn_if_unaudited logs the documented warning
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.compliance.disclosure import render_disclosure
from aegis.compliance.states import (
    StateNotAudited,
    StateNotServed,
    warn_if_unaudited,
)
from aegis.scoring.models import ScoreInput, ScoreResult


def _deal(state: str = "CA") -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state=state,
        avg_daily_balance=Decimal("12500.00"),
        true_revenue=Decimal("110000.00"),
        monthly_revenue=Decimal("110000.00"),
        lowest_balance=Decimal("3000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        fraud_score=10,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _score() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("100000.00"),
        recommended_factor_rate=Decimal("1.29"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=180,
    )


# (4) ------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["CA", "NY", "FL", "WY"])
def test_tier3_state_raises_state_not_audited(state: str) -> None:
    with pytest.raises(StateNotAudited, match=r"compliance research"):
        render_disclosure(state, _deal(state), _score())


def test_non_served_state_raises_state_not_served() -> None:
    with pytest.raises(StateNotServed, match=r"state_not_served"):
        render_disclosure("TX", _deal("TX"), _score())


def test_state_not_audited_carries_state_attr() -> None:
    try:
        render_disclosure("CA", _deal("CA"), _score())
    except StateNotAudited as exc:
        assert exc.state == "CA"
    else:
        pytest.fail("expected StateNotAudited")


# (6) ------------------------------------------------------------------------


def test_unaudited_warning_logged_for_tier3_state(caplog: pytest.LogCaptureFixture) -> None:
    """Phase 4 spec: warn_if_unaudited(state) logs the documented format."""
    with caplog.at_level(logging.WARNING, logger="aegis.compliance.states"):
        warn_if_unaudited("ny")

    matching = [
        r for r in caplog.records
        if "compliance.unaudited_state" in r.getMessage()
        and "state=NY" in r.getMessage()
        and "DEAL FROM UNAUDITED STATE" in r.getMessage()
    ]
    assert matching, (
        f"expected an unaudited-state warning; got {[r.getMessage() for r in caplog.records]}"
    )


def test_unaudited_warning_silent_for_non_served_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-served states route through API-level rejection, not unaudited-warning."""
    with caplog.at_level(logging.WARNING, logger="aegis.compliance.states"):
        warn_if_unaudited("TX")
    assert not any("compliance.unaudited_state" in r.getMessage() for r in caplog.records)


def test_unaudited_warning_silent_when_state_already_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If a state is Tier 1 or Tier 2, no warning fires.

    We promote CA to Tier 2 in this test only — the conftest fixture
    restores STATES afterwards.
    """
    from aegis.compliance import states as states_module
    from aegis.compliance.states import Tier2Regulation

    states_module.STATES["CA"] = Tier2Regulation(
        state="CA",
        state_name="California",
        verified_date=date(2026, 5, 7),
        tier=2,
        general_law_citation="(test fixture)",
        citation_url="https://example.invalid/test-only",
        notes="test-only Tier 2 entry; does not reflect actual CA law",
    )
    with caplog.at_level(logging.WARNING, logger="aegis.compliance.states"):
        warn_if_unaudited("CA")
    assert not any("compliance.unaudited_state" in r.getMessage() for r in caplog.records)
