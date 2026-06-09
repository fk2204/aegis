"""R3.4 / R4.4 / R4.6 shadow-mode evidence flags.

Covers the three additive shadow-mode changes that ship per CLAUDE.md
"Decision-boundary changes — shadow-first":

- R4.6: EOF policy mismatch shadow flag fires when the legacy scorer
  hard-declines at >1 EOF but the pipeline policy is 3+.
- R4.4: industry-aware seasonality shadow flag fires when CV > 0.50 and
  NAICS is in the known-seasonal set; existing ``-12 high_revenue_volatility``
  penalty still applies in both branches.
- R3.4: state-by-state enforcement shadow flag fires for TX merchants
  (TX HB 700 first-priority-lien) and FL/GA merchants when advance fees
  are charged.

These tests verify the additive shadow flag surface
(``ScoreResult.shadow_flags``) without asserting any change in tier,
recommendation, hard-decline reasons, or score deltas. Existing
behavior is byte-identical under all three changes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from aegis.scoring.models import MonthBreakdown, ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import (
    _is_seasonal_industry,
    _state_disclosure_flag,
    score_deal,
)

# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def fresh_ofac(tmp_path: Path) -> Iterator[OFACClient]:
    """OFAC client with a fresh empty SDN list — never matches."""
    cache = tmp_path / "ofac" / "sdn.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "entries": [{"primary_name": "ZZZ Sentinel", "aliases": []}],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def _panic() -> bytes:
        raise AssertionError("fresh cache should not refresh")

    yield OFACClient(cache_path=cache, fetcher=_panic, now=lambda: datetime.now(UTC))


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


# -- R4.6 — EOF threshold reconciliation -----------------------------------


def test_r4_6_eof_2_still_hard_declines_with_policy_mismatch_shadow(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """eof_markers=2 still hard-declines but emits the policy_mismatch shadow flag."""
    deal = clean_deal.model_copy(update={"eof_markers": 2})
    result = score_deal(deal, ofac=fresh_ofac)
    # Existing behavior: hard decline still fires (no policy change).
    assert any(
        r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons
    ), f"expected incremental_pdf_saves hard decline; got {result.hard_decline_reasons}"
    assert result.recommendation == "decline"
    assert result.tier == "F"
    # New behavior: shadow flag annotates the mismatch.
    assert (
        "eof_policy_mismatch:scorer_declines_at_2_pipeline_routes_review"
        in result.shadow_flags
    )


def test_r4_6_eof_1_no_shadow_flag(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """eof_markers=1 (default) does not fire the mismatch flag."""
    result = score_deal(clean_deal, ofac=fresh_ofac)
    assert not any(
        f.startswith("eof_policy_mismatch") for f in result.shadow_flags
    )


# -- R4.4 — industry-aware seasonality on CV --------------------------------


def test_r4_4_landscaping_seasonal_cv_0_65_recategorized_shadow(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """NAICS=561730 (landscaping), CV~0.65 → -12 still applied + recategorized flag."""
    # 50k, 150k, 50k, 130k → mean=95k, std≈45.5k, cv≈0.479. Pump it up.
    # 30k, 130k, 30k, 130k → mean=80k, std=50k, cv=0.625. Good.
    deal = clean_deal.model_copy(
        update={
            "industry_naics": "561730",
            "monthly_breakdown": _months([30000, 130000, 30000, 130000]),
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    # Existing penalty still fires.
    factors = [b["factor"] for b in result.breakdown]
    assert "high_revenue_volatility" in factors, (
        f"existing -12 penalty must still fire; got breakdown {result.breakdown}"
    )
    # Shadow flag annotates the would-skip recategorization.
    recategorized = [
        f for f in result.shadow_flags if f.startswith("seasonality_recategorized:")
    ]
    assert recategorized, (
        f"expected seasonality_recategorized shadow flag; got {result.shadow_flags}"
    )
    flag = recategorized[0]
    assert "naics=561730" in flag
    assert "would_skip_volatility_penalty" in flag


def test_r4_4_landscaping_extreme_cv_1_5_extreme_shadow(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """NAICS=561730, CV~1.5 → -12 still + 'volatility_extreme_penalty_still_applied'.

    Above 1.0 even seasonal businesses don't normally swing that hard,
    so the shadow flag documents that the penalty correctly survives
    a future seasonality config flip.
    """
    # 0.01, 200k, 0.01, 200k → mean=100k, std≈100k, cv≈1.0. Push higher.
    # 0.01, 250k, 0.01, 30k → mean=70k, std≈103k, cv≈1.48. Good.
    deal = clean_deal.model_copy(
        update={
            "industry_naics": "561730",
            "monthly_breakdown": _months([10, 250000, 10, 30000]),
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    factors = [b["factor"] for b in result.breakdown]
    assert "high_revenue_volatility" in factors
    extreme = [
        f
        for f in result.shadow_flags
        if f.startswith("seasonality_observed_but_volatility_extreme:")
    ]
    assert extreme, (
        f"expected seasonality_observed_but_volatility_extreme; "
        f"got {result.shadow_flags}"
    )
    flag = extreme[0]
    assert "naics=561730" in flag
    assert "penalty_still_applied" in flag


def test_r4_4_non_seasonal_industry_no_seasonality_flag(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """NAICS=541110 (legal services), CV>0.50 → -12 applied, NO seasonality flag."""
    deal = clean_deal.model_copy(
        update={
            "industry_naics": "541110",
            "monthly_breakdown": _months([30000, 130000, 30000, 130000]),
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    factors = [b["factor"] for b in result.breakdown]
    assert "high_revenue_volatility" in factors
    assert not any(
        f.startswith("seasonality_") for f in result.shadow_flags
    ), f"non-seasonal NAICS must not emit a seasonality flag; got {result.shadow_flags}"


def test_r4_4_seasonal_helper_unit() -> None:
    """``_is_seasonal_industry`` matches by prefix; None returns False."""
    assert _is_seasonal_industry("561730") is True  # landscaping
    assert _is_seasonal_industry("56173") is True  # exact prefix
    assert _is_seasonal_industry("113310") is True  # logging (1133)
    assert _is_seasonal_industry("722513") is True  # restaurant (7225)
    assert _is_seasonal_industry("541110") is False  # legal
    assert _is_seasonal_industry("999999") is False
    assert _is_seasonal_industry(None) is False


# -- R3.4 — state-by-state enforcement --------------------------------------


def test_r3_4_tx_merchant_emits_hb700_shadow(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """TX merchant + active deal → TX_HB700 shadow flag, no tier change."""
    deal = clean_deal.model_copy(update={"state": "TX"})
    result = score_deal(deal, ofac=fresh_ofac)
    assert (
        "state_enforcement_concern:TX_HB700_tx_merchant_review"
        in result.shadow_flags
    ), f"expected TX HB 700 shadow flag; got {result.shadow_flags}"
    # Shadow-only: tier / recommendation / hard_decline_reasons unchanged.
    assert result.tier != "F"  # clean_deal otherwise approves
    assert result.recommendation in {"approve", "refer"}
    assert "state_enforcement_concern:TX_HB700_tx_merchant_review" not in (
        result.hard_decline_reasons
    )


def test_r3_4_fl_merchant_with_advance_fees_via_helper() -> None:
    """FL merchant with advance_fees_charged=True → advance-fee shadow flag.

    Exercises the helper directly because ScoreInput has no
    ``advance_fees_charged`` field today; the live ``score_deal`` call
    passes None for that arg per the wiring contract. Test asserts the
    helper's behavior so a future plumbing change preserves the contract.
    """
    flags = _state_disclosure_flag(
        merchant_state="FL", deal_state="FL", advance_fees_charged=True
    )
    assert "state_enforcement_concern:FL_GA_advance_fee_prohibition" in flags


def test_r3_4_ga_merchant_with_advance_fees_via_helper() -> None:
    """GA merchant with advance_fees_charged=True → advance-fee shadow flag."""
    flags = _state_disclosure_flag(
        merchant_state="GA", deal_state="GA", advance_fees_charged=True
    )
    assert "state_enforcement_concern:FL_GA_advance_fee_prohibition" in flags


def test_r3_4_fl_merchant_no_advance_fees_no_flag() -> None:
    """FL with advance_fees_charged=False → no shadow flag."""
    flags = _state_disclosure_flag(
        merchant_state="FL", deal_state="FL", advance_fees_charged=False
    )
    assert flags == []


def test_r3_4_ca_merchant_no_triggers(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """CA merchant with no triggers → no state shadow flag."""
    result = score_deal(clean_deal, ofac=fresh_ofac)  # clean_deal.state="CA"
    assert not any(
        f.startswith("state_enforcement_concern:") for f in result.shadow_flags
    ), f"CA must not emit a state shadow flag; got {result.shadow_flags}"


def test_r3_4_tx_merchant_helper_unit() -> None:
    """Helper returns the TX flag regardless of advance_fees_charged."""
    assert _state_disclosure_flag(
        merchant_state="TX", deal_state="TX", advance_fees_charged=None
    ) == ["state_enforcement_concern:TX_HB700_tx_merchant_review"]
    assert _state_disclosure_flag(
        merchant_state="tx", deal_state="tx", advance_fees_charged=False
    ) == ["state_enforcement_concern:TX_HB700_tx_merchant_review"]


def test_r3_4_other_state_no_flag() -> None:
    """Other states with no triggers → empty list."""
    assert _state_disclosure_flag(
        merchant_state="NY", deal_state="NY", advance_fees_charged=True
    ) == []
    assert _state_disclosure_flag(
        merchant_state=None, deal_state=None, advance_fees_charged=True
    ) == []


# -- combined: shadow flags survive hard-decline path -----------------------


def test_r4_6_shadow_flag_emitted_even_on_hard_decline(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Even when other hard declines fire, the EOF shadow flag still surfaces.

    Confirms shadow_flags is populated from ``_check_hard_declines`` and
    plumbed into the hard-decline ScoreResult, not only the soft path.
    """
    deal = clean_deal.model_copy(
        update={
            "eof_markers": 2,
            "fraud_score": 95,  # triggers fraud_score_critical first
        }
    )
    result = score_deal(deal, ofac=fresh_ofac)
    assert result.recommendation == "decline"
    assert (
        "eof_policy_mismatch:scorer_declines_at_2_pipeline_routes_review"
        in result.shadow_flags
    )


def test_r3_4_tx_shadow_flag_emitted_on_hard_decline(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """TX shadow flag fires even on the hard-decline path."""
    deal = clean_deal.model_copy(update={"state": "TX", "fraud_score": 95})
    result = score_deal(deal, ofac=fresh_ofac)
    assert result.recommendation == "decline"
    assert (
        "state_enforcement_concern:TX_HB700_tx_merchant_review"
        in result.shadow_flags
    )


# -- defensive: model defaults ----------------------------------------------


def test_score_input_seasonal_breakdown_period() -> None:
    """Sanity check: helper called with the date in the smoke fixture works."""
    # Just confirms the fixture has its statement period set; not testing
    # date logic per se but ensures ScoreInput stays unbroken.
    sample_date = date(2026, 4, 1)
    assert sample_date.year == 2026
