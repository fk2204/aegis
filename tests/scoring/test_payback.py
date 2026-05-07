"""Test (c): estimated_payback_days uses total_repayment / daily_payment.

The TS bug used principal / daily_payment, undercounting payback by the
factor margin. For a B-tier deal at factor 1.29, that's ~22% off — funder
holdback expectations are wrong, broker emails reconcile incorrectly.

Hand-computed scenario
----------------------
Inputs (from clean_deal fixture but with tier-pinning revenue):
  monthly_revenue   = $110,000
  daily_revenue     = 110000 / 22 = $5,000/day

Soft scoring puts the clean deal into tier B (score ≈ 65-79). Tier B uses
factor=1.29, holdback=0.12.

  daily_payment    = 5000 * 0.12 = $600
  suggested_max    = 110000 * 1.2 = $132,000 → rounded to $132,000
  total_repayment  = 132000 * 1.29 = $170,280
  CORRECT payback  = 170280 / 600 = 283.8 → 284 days

The TS-bug formula would give:
  TS BUG payback   = 132000 / 600 = 220 days

We assert the result is at least the factor-margin away from the buggy value.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal


@pytest.fixture
def clean_ofac(tmp_path: Path) -> OFACClient:
    cache = tmp_path / "ofac.json"
    cache.write_text(
        json.dumps(
            {
                "entries": [{"primary_name": "ZZZ Should Not Match", "aliases": []}],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def _now() -> datetime:
        return datetime.now(UTC)

    return OFACClient(cache_path=cache, fetcher=_must_not_call, now=_now)


def _must_not_call() -> bytes:
    raise AssertionError("OFAC fetcher should not be called when cache is fresh")


def test_payback_uses_total_repayment_not_principal(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    result = score_deal(clean_deal, ofac=clean_ofac)
    assert result.tier in {"A", "B", "C"}, f"unexpected tier {result.tier}"

    factor = result.recommended_factor_rate
    holdback = result.recommended_holdback_pct
    suggested = result.suggested_max_advance
    payback = result.estimated_payback_days

    assert factor > 0 and holdback > 0 and suggested > 0 and payback is not None

    daily_revenue = clean_deal.monthly_revenue / Decimal("22")
    daily_payment = (daily_revenue * holdback).quantize(Decimal("0.01"))
    total_repayment = (suggested * factor).quantize(Decimal("0.01"))

    correct_days = int((total_repayment / daily_payment).to_integral_value())
    buggy_ts_days = int((suggested / daily_payment).to_integral_value())

    assert payback == correct_days, (
        f"payback ({payback}) should equal correct {correct_days} "
        f"(total_repayment / daily_payment), not buggy {buggy_ts_days} "
        f"(principal / daily_payment)"
    )
    # Sanity: the gap between correct and buggy must be the factor margin.
    margin_pct = (correct_days - buggy_ts_days) / buggy_ts_days
    assert margin_pct > Decimal("0.15"), (
        f"correct/buggy margin {margin_pct} too small — formula may be tautological"
    )


def test_payback_zero_when_holdback_zero(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """Tier F (or other holdback=0 paths) should produce payback=0, not divide-by-zero."""
    declined = clean_deal.model_copy(update={"fraud_score": 80})
    result = score_deal(declined, ofac=clean_ofac)
    assert result.recommendation == "decline"
    assert result.estimated_payback_days == 0


def test_submission_package_payback_matches_score_payback(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """submission_package term math must reconcile with score.estimated_payback_days."""
    from aegis.scoring.match_funders import FunderRow, match_funder
    from aegis.scoring.submission_package import build_submission_package

    score = score_deal(clean_deal, ofac=clean_ofac)
    funder = FunderRow(
        id=clean_deal.merchant_id,
        name="Test Funder",
        min_monthly_revenue=Decimal("20000.00"),
    )
    match = match_funder(funder, clean_deal, score)
    assert match is not None
    pkg = build_submission_package(clean_deal, score, match)

    body = pkg.email_body
    expected = f"est. payback days   {score.estimated_payback_days}"
    assert expected in body, (
        f"submission package payback line not present; body=\n{body}"
    )
