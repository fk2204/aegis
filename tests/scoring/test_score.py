"""Hard-decline tests — every named rule fires when its condition is met.

Demonstrates Phase 3 review criterion (a): hard declines fire correctly,
including the new `ofac_sanctions_match` reason.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal


@pytest.fixture
def fresh_ofac(tmp_path: Path) -> Iterator[OFACClient]:
    """OFAC client with a SDN list containing one obvious match. Cache <24h old."""
    cache = tmp_path / "ofac" / "sdn.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "primary_name": "Putin, Vladimir Vladimirovich",
                        "aliases": ["Vladimir Putin"],
                    },
                    {"primary_name": "Sanctioned Front Co", "aliases": []},
                ],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def _now() -> datetime:
        return datetime.now(UTC)

    client = OFACClient(cache_path=cache, fetcher=_panic_fetcher, now=_now)
    yield client


def _panic_fetcher() -> bytes:
    raise RuntimeError("fetcher should not be called when cache is fresh")


def test_clean_deal_does_not_decline(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    result = score_deal(clean_deal, ofac=fresh_ofac)
    assert result.hard_decline_reasons == [], (
        f"clean deal should not hard-decline; got {result.hard_decline_reasons}"
    )
    assert result.recommendation in {"approve", "refer"}


def test_ofac_business_name_match_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"business_name": "Sanctioned Front Co"})
    result = score_deal(deal, ofac=fresh_ofac)
    assert "ofac_sanctions_match" in result.hard_decline_reasons
    assert result.recommendation == "decline"
    assert result.tier == "F"


def test_ofac_owner_name_match_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(
        update={"owner_name": "Owned by Vladimir Putin and family"}
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert "ofac_sanctions_match" in result.hard_decline_reasons


def test_ofac_clean_name_does_not_match(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    result = score_deal(clean_deal, ofac=fresh_ofac)
    assert "ofac_sanctions_match" not in result.hard_decline_reasons


def test_stacking_exceeds_limit_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"mca_positions": 3})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("stacking_exceeds_limit") for r in result.hard_decline_reasons)


def test_debt_to_revenue_over_40pct_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"debt_to_revenue": Decimal("0.45")})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(
        r.startswith("debt_to_revenue_exceeds_40pct") for r in result.hard_decline_reasons
    )


def test_fraud_score_critical_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"fraud_score": 75})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("fraud_score_critical") for r in result.hard_decline_reasons)


def test_fraud_score_critical_aligned_with_pipeline_threshold() -> None:
    """Regression: the scorer's hard-decline floor must match the pipeline's.

    Pre-fix (audit doc §A.2) the scorer used 70 while the pipeline used 65.
    A deal with fraud_score in [65, 69] then landed in a split state: pipeline
    routed it to manual_review but the scorer ran soft scoring as if nothing
    was wrong. Pin them aligned.
    """
    from aegis.parser.pipeline import HARD_DECLINE_THRESHOLD
    from aegis.scoring.score import FRAUD_SCORE_HARD_DECLINE

    assert FRAUD_SCORE_HARD_DECLINE == HARD_DECLINE_THRESHOLD


def test_fraud_score_at_pipeline_threshold_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """A deal at exactly `HARD_DECLINE_THRESHOLD` must hit `fraud_score_critical`.

    Boundary check for the A.2 fix: pre-fix this deal went through soft
    scoring; post-fix it hard-declines like the pipeline already does.
    """
    from aegis.parser.pipeline import HARD_DECLINE_THRESHOLD

    deal = clean_deal.model_copy(update={"fraud_score": HARD_DECLINE_THRESHOLD})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("fraud_score_critical") for r in result.hard_decline_reasons)


def test_eof_markers_declines(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    deal = clean_deal.model_copy(update={"eof_markers": 3})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons)


def test_revenue_below_minimum_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"monthly_revenue": Decimal("8000.00")})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("revenue_below_minimum") for r in result.hard_decline_reasons)


def test_industry_excluded_declines(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    deal = clean_deal.model_copy(update={"industry_risk_tier": "avoid"})
    result = score_deal(deal, ofac=fresh_ofac)
    assert "industry_excluded" in result.hard_decline_reasons


def test_days_negative_over_15_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"days_negative": 16})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("days_negative_gt_15") for r in result.hard_decline_reasons)


def test_nsf_count_over_10_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"num_nsf": 10})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("nsf_count_gte_10") for r in result.hard_decline_reasons)


def test_returned_ach_over_5_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"returned_ach_count": 6})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("returned_ach_gt_5") for r in result.hard_decline_reasons)


def test_tib_under_3_declines(clean_deal: ScoreInput, fresh_ofac: OFACClient) -> None:
    deal = clean_deal.model_copy(update={"time_in_business_months": 2})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("tib_under_3") for r in result.hard_decline_reasons)


def test_validation_failed_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(update={"validation_passed": False})
    result = score_deal(deal, ofac=fresh_ofac)
    assert "validation_failed_manual_review_required" in result.hard_decline_reasons


def test_prior_default_renewal_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(
        update={"is_renewal": True, "prior_payoff_performance": "default"}
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert "prior_default" in result.hard_decline_reasons


def test_missing_credit_and_tib_become_soft_concerns(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    deal = clean_deal.model_copy(
        update={"credit_score": None, "time_in_business_months": None}
    )
    result = score_deal(deal, ofac=fresh_ofac)
    # Not hard declines; surfaced as soft_concerns.
    assert "missing_credit_score" in result.soft_concerns
    assert "missing_time_in_business" in result.soft_concerns


def test_f_tier_soft_decline_distinguishable_from_hard_decline(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """A merchant whose soft score sinks to F (no hard rule fired) carries
    a distinct soft_concern, so the merchant detail page can explain the
    reason was 'low aegis score' rather than 'stacking' or 'fraud'.
    """
    # Force a low aggregate score: weak balance + chronic negative + low credit.
    # No hard-decline trigger (NSF/days_negative/TIB stay safe).
    deal = clean_deal.model_copy(
        update={
            "monthly_revenue": Decimal("11000.00"),  # just above min ($10k)
            "true_revenue": Decimal("11000.00"),
            "avg_daily_balance": Decimal("400.00"),  # very weak (-12)
            "lowest_balance": Decimal("-50.00"),  # went negative (-5)
            "days_negative": 7,  # chronic negative (-10) but ≤ 15
            "credit_score": 540,  # poor (-15)
            "time_in_business_months": 4,  # 6-12 mo (-8) but ≥ 3
            "fraud_score": 35,  # low_fraud_signals (-8) but < 65
            "industry_risk_tier": "high",  # -10
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert result.hard_decline_reasons == [], (
        "this scenario must NOT hit any hard-decline rule"
    )
    assert result.tier == "F", f"score should sink to F; got {result.tier}"
    assert result.recommendation == "decline"
    assert any(
        c.startswith("soft_score_below_threshold") for c in result.soft_concerns
    ), f"expected soft_score_below_threshold; saw {result.soft_concerns}"


def test_hard_decline_does_not_emit_soft_threshold_concern(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """When a hard decline fires, the soft-threshold marker should NOT also
    appear (we never run soft scoring on hard-declined deals).
    """
    deal = clean_deal.model_copy(update={"mca_positions": 5})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(r.startswith("stacking_exceeds_limit") for r in result.hard_decline_reasons)
    assert not any(
        c.startswith("soft_score_below_threshold") for c in result.soft_concerns
    ), "soft-threshold marker should not appear alongside a hard decline"
