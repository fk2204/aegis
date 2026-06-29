"""Tests for ``aegis.scoring_v2.sba_eligibility.check_sba_eligibility``.

Coverage matches the master plan § 12.1 rule set:

* Hard disqualifiers — ``bankruptcy_active=True`` and
  ``ofac_is_clear=False`` each independently force ``eligible=False``
  with the exact human-readable reason on ``blockers``.
* Soft thresholds — TIB / FICO / monthly revenue. Each fires a blocker
  when below the floor; each fires a strength when above the upper
  band.
* Program selection — when eligible, the tier order is
  ``7(a)`` (revenue > $100k AND FICO ≥ 680) → ``Express``
  (revenue > $50k) → ``Microloan`` (the rest).
* Estimated max — ``revenue * 36`` when eligible. ``None`` when not.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.sba_eligibility import (
    SBAEligibilityResult,
    check_sba_eligibility,
)
from aegis.storage import AnalysisRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _merchant(
    *,
    time_in_business_months: int | None = 60,
    credit_score: int | None = 720,
    bankruptcy_active: bool | None = False,
    ofac_is_clear: bool | None = True,
) -> MerchantRow:
    """Build a MerchantRow that, with default args, clears every SBA gate."""
    return MerchantRow(
        business_name="Rendezvous Inc",
        state="CA",
        status="finalized",
        time_in_business_months=time_in_business_months,
        credit_score=credit_score,
        bankruptcy_active=bankruptcy_active,
        ofac_is_clear=ofac_is_clear,
    )


def _analysis(monthly_revenue: Decimal) -> AnalysisRow:
    """Build a minimal AnalysisRow with the given monthly revenue."""
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=uuid4(),
        statement_period_start=date(2026, 5, 1),
        statement_period_end=date(2026, 5, 31),
        statement_days=31,
        beginning_balance=Decimal("0.00"),
        ending_balance=Decimal("0.00"),
        avg_daily_balance=Decimal("0.00"),
        true_revenue=monthly_revenue,
        monthly_revenue=monthly_revenue,
        lowest_balance=Decimal("0.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
    )


# ---------------------------------------------------------------------------
# Hard disqualifiers
# ---------------------------------------------------------------------------


def test_active_bankruptcy_blocks_eligibility() -> None:
    result = check_sba_eligibility(
        _merchant(bankruptcy_active=True),
        _analysis(Decimal("75000")),
    )
    assert isinstance(result, SBAEligibilityResult)
    assert result.eligible is False
    assert result.program is None
    assert result.estimated_max_amount is None
    assert "Active bankruptcy on file" in result.blockers


def test_ofac_flag_blocks_eligibility() -> None:
    result = check_sba_eligibility(
        _merchant(ofac_is_clear=False),
        _analysis(Decimal("75000")),
    )
    assert result.eligible is False
    assert result.program is None
    assert result.estimated_max_amount is None
    assert "OFAC flag — cannot place with any lender" in result.blockers


# ---------------------------------------------------------------------------
# Time in business
# ---------------------------------------------------------------------------


def test_tib_under_24_months_is_blocker() -> None:
    result = check_sba_eligibility(
        _merchant(time_in_business_months=18),
        _analysis(Decimal("75000")),
    )
    assert result.eligible is False
    assert any("Time in business < 24 months" in b for b in result.blockers)


def test_tib_60_plus_months_is_strength() -> None:
    result = check_sba_eligibility(
        _merchant(time_in_business_months=60),
        _analysis(Decimal("75000")),
    )
    assert result.eligible is True
    assert any("Time in business ≥ 60 months" in s for s in result.strengths)


# ---------------------------------------------------------------------------
# FICO
# ---------------------------------------------------------------------------


def test_fico_under_650_is_blocker() -> None:
    result = check_sba_eligibility(
        _merchant(credit_score=620),
        _analysis(Decimal("75000")),
    )
    assert result.eligible is False
    assert any("FICO < 650" in b for b in result.blockers)


def test_fico_700_plus_is_strength() -> None:
    result = check_sba_eligibility(
        _merchant(credit_score=700),
        _analysis(Decimal("75000")),
    )
    assert result.eligible is True
    assert any("FICO ≥ 700" in s for s in result.strengths)


# ---------------------------------------------------------------------------
# Revenue
# ---------------------------------------------------------------------------


def test_revenue_under_10k_is_blocker() -> None:
    result = check_sba_eligibility(
        _merchant(),
        _analysis(Decimal("8000")),
    )
    assert result.eligible is False
    assert "Revenue too low for most SBA programs" in result.blockers


def test_revenue_over_50k_is_strength() -> None:
    result = check_sba_eligibility(
        _merchant(),
        _analysis(Decimal("60000")),
    )
    assert result.eligible is True
    assert any("Monthly revenue > $50,000" in s for s in result.strengths)


# ---------------------------------------------------------------------------
# Program tier selection
# ---------------------------------------------------------------------------


def test_clean_high_revenue_high_fico_selects_7a() -> None:
    result = check_sba_eligibility(
        _merchant(credit_score=700),
        _analysis(Decimal("120000")),
    )
    assert result.eligible is True
    assert result.program == "7(a)"


def test_clean_mid_revenue_selects_express() -> None:
    result = check_sba_eligibility(
        _merchant(credit_score=720),
        _analysis(Decimal("60000")),
    )
    assert result.eligible is True
    assert result.program == "Express"


def test_clean_low_revenue_selects_microloan() -> None:
    result = check_sba_eligibility(
        _merchant(credit_score=720),
        _analysis(Decimal("20000")),
    )
    assert result.eligible is True
    assert result.program == "Microloan"


# ---------------------------------------------------------------------------
# Estimated max amount
# ---------------------------------------------------------------------------


def test_estimated_max_is_revenue_times_36() -> None:
    revenue = Decimal("75000")
    result = check_sba_eligibility(_merchant(), _analysis(revenue))
    assert result.eligible is True
    assert result.estimated_max_amount == revenue * Decimal("36")


def test_estimated_max_is_none_when_ineligible() -> None:
    result = check_sba_eligibility(
        _merchant(bankruptcy_active=True),
        _analysis(Decimal("75000")),
    )
    assert result.estimated_max_amount is None
