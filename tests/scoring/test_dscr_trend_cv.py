"""DSCR + revenue trend + revenue volatility scoring."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from aegis.scoring.models import MonthBreakdown, ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal


@pytest.fixture
def fresh_ofac(tmp_path: Path) -> OFACClient:
    cache = tmp_path / "sdn.json"
    cache.write_text(
        json.dumps(
            {
                "entries": [{"primary_name": "ZZZ Sentinel", "aliases": []}],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def panic() -> bytes:
        raise AssertionError("fresh cache should not refresh")

    return OFACClient(cache_path=cache, fetcher=panic, now=lambda: datetime.now(UTC))


# -- DSCR --------------------------------------------------------------------


def test_dscr_below_1_hard_declines(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    """monthly_revenue=$110k, monthly_obligations=$80k, daily=$2k -> DSCR=0.92."""
    deal = clean_deal.model_copy(
        update={
            "total_monthly_obligations": Decimal("80000.00"),
            "proposed_daily_payment": Decimal("2000.00"),
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("dscr_below_1") for r in result.hard_decline_reasons), (
        f"expected dscr_below_1; got {result.hard_decline_reasons}"
    )


def test_dscr_strong_adds_to_score(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    """monthly_revenue=$110k, monthly_obligations=$22k, daily=$1k -> DSCR=2.5 (strong)."""
    deal = clean_deal.model_copy(
        update={
            "total_monthly_obligations": Decimal("22000.00"),
            "proposed_daily_payment": Decimal("1000.00"),
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert result.hard_decline_reasons == []
    assert any(b["factor"] == "dscr_strong" for b in result.breakdown)


def test_dscr_tight_negative_score(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    """DSCR between 1.0 and 1.15 -> -15 'dscr_tight'."""
    # 110000 / X >= 1.0 and < 1.15 → X between 95652 and 110000.
    # Use obligations=70000, daily=$1500 → 70000+33000=103000, dscr ≈ 1.068
    deal = clean_deal.model_copy(
        update={
            "total_monthly_obligations": Decimal("70000.00"),
            "proposed_daily_payment": Decimal("1500.00"),
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert result.hard_decline_reasons == []
    assert any(b["factor"] == "dscr_tight" for b in result.breakdown)


def test_dscr_missing_inputs_skipped(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    """No DSCR inputs = no DSCR scoring (and no hard decline either)."""
    result = score_deal(clean_deal, ofac=fresh_ofac)
    factors = [b["factor"] for b in result.breakdown]
    assert "dscr_strong" not in factors
    assert "dscr_adequate" not in factors
    assert "dscr_tight" not in factors


# -- Revenue trend -----------------------------------------------------------


def _months(values: list[int]) -> list[MonthBreakdown]:
    """Build a list of MonthBreakdown from deposit totals."""
    return [
        MonthBreakdown(
            month=f"2026-{i + 1:02d}",
            deposits=Decimal(v),
            withdrawals=Decimal("0.00"),
            avg_balance=Decimal("5000.00"),
        )
        for i, v in enumerate(values)
    ]


def test_revenue_declining_15pct_penalizes(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Last 3 months: 100k -> 90k -> 80k = -20% trend."""
    deal = clean_deal.model_copy(update={"monthly_breakdown": _months([100000, 90000, 80000])})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(b["factor"] == "revenue_declining_15pct+" for b in result.breakdown)


def test_revenue_growing_10pct_rewards(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Last 3 months: 80k -> 88k -> 96k = +20% trend."""
    deal = clean_deal.model_copy(update={"monthly_breakdown": _months([80000, 88000, 96000])})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(b["factor"] == "revenue_growing_10pct+" for b in result.breakdown)


def test_revenue_flat_no_trend_score(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Last 3 months flat -> neither +8 nor -15."""
    deal = clean_deal.model_copy(update={"monthly_breakdown": _months([100000, 102000, 100500])})
    result = score_deal(deal, ofac=fresh_ofac)
    factors = [b["factor"] for b in result.breakdown]
    assert "revenue_growing_10pct+" not in factors
    assert "revenue_declining_15pct+" not in factors


# -- Volatility (CV) ---------------------------------------------------------


def test_high_volatility_penalizes(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    """4 months with cv > 0.50."""
    # 10k, 100k, 10k, 100k → mean=55k, std≈45k, cv≈0.82
    deal = clean_deal.model_copy(
        update={"monthly_breakdown": _months([10000, 100000, 10000, 100000])}
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(b["factor"] == "high_revenue_volatility" for b in result.breakdown)


def test_stable_revenue_rewards(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    """4 months with cv ≤ 0.20."""
    deal = clean_deal.model_copy(
        update={"monthly_breakdown": _months([100000, 102000, 98000, 101000])}
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(b["factor"] == "stable_revenue" for b in result.breakdown)


def test_volatility_skipped_under_4_months(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"monthly_breakdown": _months([100000, 80000, 90000])})
    result = score_deal(deal, ofac=fresh_ofac)
    factors = [b["factor"] for b in result.breakdown]
    assert "high_revenue_volatility" not in factors
    assert "stable_revenue" not in factors
