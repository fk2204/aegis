"""Live tier matrix folded into ``match_funder``.

Pre-fix: ``evaluate_tier_matches`` ran in shadow mode — the per-tier
qualification result was attached to ``FunderMatch.tier_matches`` but
never reached ``match_score`` or ``reasons``. Funders with a
published tier matrix (Elite / A / B / C with per-tier FICO + TIB
+ revenue floors) were matched against the funder-level minimums
only, so the operator never saw "Qualifies at Tier B" — just the
score.

Post-fix: when ``funder.tiers`` is non-empty, the per-tier
evaluation drives the qualification decision and the match_score.
``no_qualifying_tier`` hard-fails the match when no tier accepts
the deal; the qualifying tier name lands in ``reasons`` so the
dossier reads "Qualifies at Tier B" instead of just a number.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from aegis.funders.models import FunderRow, FunderTier
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _deal(**overrides: object) -> ScoreInput:
    """Baseline deal — qualifies on all funder-wide gates; per-test
    overrides flex the tier-axis criteria (FICO, TIB, revenue,
    positions)."""
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="811111",
        industry_choice="Auto Repair / Service",
        time_in_business_months=36,
        credit_score=650,
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
    return base.model_copy(update=overrides)


def _score(*, tier: Literal["A", "B", "C", "D", "F"] = "B") -> ScoreResult:
    return ScoreResult(
        score=75,
        tier=tier,
        recommendation="approve",
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def _tiered_funder(**overrides: object) -> FunderRow:
    """Funder with a three-tier matrix (Elite / B / C). Convention:
    best-tier-first, so Elite at index 0, C at index 2."""
    base = FunderRow(
        name="Tiered Test Funder",
        tiers=(
            FunderTier(
                name="Elite",
                buy_rate_low=Decimal("1.18"),
                buy_rate_high=Decimal("1.22"),
                min_credit_score=700,
                min_months_in_business=24,
                min_monthly_revenue=Decimal("75000.00"),
                max_positions=1,
            ),
            FunderTier(
                name="B",
                buy_rate_low=Decimal("1.25"),
                buy_rate_high=Decimal("1.32"),
                min_credit_score=600,
                min_months_in_business=12,
                min_monthly_revenue=Decimal("50000.00"),
                max_positions=2,
            ),
            FunderTier(
                name="C",
                buy_rate_low=Decimal("1.35"),
                buy_rate_high=Decimal("1.42"),
                min_credit_score=550,
                min_months_in_business=6,
                min_monthly_revenue=Decimal("25000.00"),
                max_positions=3,
            ),
        ),
    )
    return base.model_copy(update=overrides)


# ─────────────────────────────────────────────────────────────────────
# Qualifying-tier surfaced in reasons + match_score
# ─────────────────────────────────────────────────────────────────────


def test_qualifies_at_top_tier_when_merchant_meets_elite_floor() -> None:
    """Strong merchant: FICO 750, TIB 36mo, revenue $110k. Lands at
    Elite (position 0). match_score base 90; ``qualifies_at_tier:Elite``
    surfaces in reasons."""
    funder = _tiered_funder()
    deal = _deal(credit_score=750)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 90
    assert "qualifies_at_tier:Elite" in match.reasons


def test_qualifies_at_middle_tier_when_elite_fails_one_axis() -> None:
    """Merchant with FICO 650 fails Elite (700 floor) but passes B
    (600 floor). Lands at B (position 1). match_score base 75."""
    funder = _tiered_funder()
    deal = _deal(credit_score=650)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 75
    assert "qualifies_at_tier:B" in match.reasons
    assert "qualifies_at_tier:Elite" not in match.reasons


def test_qualifies_at_bottom_tier_when_only_c_floor_met() -> None:
    """Marginal merchant: FICO 560, TIB 8mo, revenue $30k. Only C
    accepts; match_score base 60."""
    funder = _tiered_funder()
    deal = _deal(
        credit_score=560,
        time_in_business_months=8,
        monthly_revenue=Decimal("30000.00"),
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 60
    assert "qualifies_at_tier:C" in match.reasons


def test_no_qualifying_tier_hard_fails_when_all_tiers_reject() -> None:
    """FICO 500 fails every tier's credit floor (700 / 600 / 550).
    Hard fail with ``no_qualifying_tier``. match_score = 0."""
    funder = _tiered_funder()
    deal = _deal(credit_score=500)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 0
    assert any("no_qualifying_tier" in c for c in match.soft_concerns)
    assert "qualifies_at_tier:Elite" not in match.reasons


# ─────────────────────────────────────────────────────────────────────
# Funder-level minimums superseded by tier matrix
# ─────────────────────────────────────────────────────────────────────


def test_funder_level_minimums_skipped_when_tiers_present() -> None:
    """Funder has both tier matrix AND funder-level ``min_credit_score=800``.
    Merchant with FICO 650 should still qualify at B (per tier matrix),
    NOT hard-fail against the funder-level 800. The tier matrix
    supersedes."""
    funder = _tiered_funder(min_credit_score=800)
    deal = _deal(credit_score=650)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 75  # B-tier base
    assert "qualifies_at_tier:B" in match.reasons
    assert not any("credit" in c and "min 800" in c for c in match.soft_concerns)


def test_funder_wide_gates_still_apply_with_tiers() -> None:
    """Tier matrix supersedes revenue/credit/TIB/positions, but
    funder-wide gates (excluded_industries, excluded_states, ADB)
    still apply. Excluded restaurant industry should hard-fail even
    when tiers would accept the deal."""
    funder = _tiered_funder(excluded_industries=("restaurant",))
    deal = _deal(
        credit_score=750,
        industry_choice="Restaurant / Food Service",
        industry_naics="722511",
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 0
    assert any("industry_excluded" in c for c in match.soft_concerns)


# ─────────────────────────────────────────────────────────────────────
# Funder without tiers — existing behaviour unchanged
# ─────────────────────────────────────────────────────────────────────


def test_funder_without_tiers_uses_funder_level_minimums() -> None:
    """When ``funder.tiers`` is empty, the existing funder-level
    minimums drive the gates and no ``qualifies_at_tier:*`` reason
    appears. Likelihood follows ``_likelihood(score.tier)``."""
    funder = FunderRow(
        name="Flat Funder",
        min_credit_score=600,
        min_monthly_revenue=Decimal("50000.00"),
    )
    deal = _deal(credit_score=650)
    match = match_funder(funder, deal, _score(tier="B"))
    assert match is not None
    # B-tier base 75 - 0 soft = 75
    assert match.match_score == 75
    assert "tier_B" in match.reasons
    assert not any(r.startswith("qualifies_at_tier:") for r in match.reasons)


# ─────────────────────────────────────────────────────────────────────
# Soft concerns still discount match_score
# ─────────────────────────────────────────────────────────────────────


def test_tier_match_score_discounts_per_soft_concern() -> None:
    """Tier base score is reduced by 10 per soft concern, matching
    the funder-level ``_likelihood`` curve so the dossier comparison
    between tier-funders and flat-funders stays calibrated.

    Build a deal that qualifies at Elite but raises one soft concern
    (existing MCA position triggers ``stacking_acceptance_unconfirmed``).
    Expected: base 90 - 10 = 80.
    """
    funder = _tiered_funder()
    deal = _deal(credit_score=750, mca_positions=1)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 80
    assert "qualifies_at_tier:Elite" in match.reasons


# ─────────────────────────────────────────────────────────────────────
# tier_matches surface still populated for the dossier detail panel
# ─────────────────────────────────────────────────────────────────────


def test_tier_matches_array_still_populated_for_dossier_detail() -> None:
    """The full per-tier breakdown is still attached to ``tier_matches``
    so the dossier detail panel can show 'Elite: rejected (FICO 650 <
    min 700)', 'B: qualified', 'C: qualified' even when the headline
    qualifies_at_tier reflects B."""
    funder = _tiered_funder()
    deal = _deal(credit_score=650)
    match = match_funder(funder, deal, _score())
    assert match is not None
    tier_match_names = [tm.tier_name for tm in match.tier_matches]
    assert tier_match_names == ["Elite", "B", "C"]
    # Elite rejected, B+C qualified.
    by_name = {tm.tier_name: tm for tm in match.tier_matches}
    assert by_name["Elite"].qualifies is False
    assert by_name["B"].qualifies is True
    assert by_name["C"].qualifies is True
