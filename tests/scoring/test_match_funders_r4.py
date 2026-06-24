"""R4.2 + R4.3 tests for match_funders.

R4.2 — pricing guidance derived from funder ``typical_factor_*`` and
``typical_holdback_*`` envelope. Validates linear tier-driven
interpolation, advance clamping, APR computation, and the APR-failure
None-fallback (R0.4 discipline: never silently render 0%).

R4.3 — auto_decline_conditions + conditional_requirements wiring. The
matcher previously ignored both fields; tests here confirm the matcher
now surfaces them. Auto-decline classification is keyword-driven (not
NL parsed): absolute-language entries become hard fails, everything
else surfaces as a soft concern so the operator reviews.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest

from aegis.compliance import apr as apr_module
from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import (
    compute_estimated_terms,
    evaluate_tier_matches,
    match_funder,
)
from aegis.scoring.models import ScoreInput, ScoreResult
from aegis.scoring_v2.offer import OfferRecommendation

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _deal(**overrides: object) -> ScoreInput:
    """Baseline ScoreInput. Overrides one field at a time per test."""
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="722511",
        time_in_business_months=36,
        credit_score=700,
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


def _score(
    *,
    tier: Literal["A", "B", "C", "D", "F"] = "C",
    suggested_max_advance: Decimal = Decimal("50000.00"),
    estimated_payback_days: int | None = 120,
) -> ScoreResult:
    return ScoreResult(
        score=60,
        tier=tier,
        recommendation="approve",
        suggested_max_advance=suggested_max_advance,
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=estimated_payback_days,
    )


def _funder_with_pricing(**overrides: object) -> FunderRow:
    base = FunderRow(
        id=uuid4(),
        name="Pricing Co",
        min_monthly_revenue=Decimal("25000.00"),
        min_advance=Decimal("5000.00"),
        max_advance=Decimal("250000.00"),
        typical_factor_low=Decimal("1.20"),
        typical_factor_high=Decimal("1.40"),
        typical_holdback_low=Decimal("0.10"),
        typical_holdback_high=Decimal("0.20"),
    )
    return base.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# R4.2 — pricing guidance
# ---------------------------------------------------------------------------


def test_r42_tier_c_interpolates_to_50_percent() -> None:
    """tier=C → position 0.50 → midpoint of factor + holdback ranges."""
    funder = _funder_with_pricing()
    deal = _deal()
    score = _score(tier="C")
    terms = compute_estimated_terms(funder, score, deal)
    assert terms is not None
    # midpoint of [1.20, 1.40] is 1.30
    assert terms.estimated_factor == Decimal("1.3000")
    # midpoint of [0.10, 0.20] is 0.15
    assert terms.estimated_holdback_pct == Decimal("0.1500")
    assert "tier=C" in terms.interpolation_evidence
    assert "50%" in terms.interpolation_evidence


def test_r42_tier_a_interpolates_to_low_end() -> None:
    funder = _funder_with_pricing()
    terms = compute_estimated_terms(funder, _score(tier="A"), _deal())
    assert terms is not None
    assert terms.estimated_factor == Decimal("1.2000")
    assert terms.estimated_holdback_pct == Decimal("0.1000")


def test_r42_tier_f_interpolates_to_high_end() -> None:
    funder = _funder_with_pricing()
    terms = compute_estimated_terms(funder, _score(tier="F"), _deal())
    assert terms is not None
    assert terms.estimated_factor == Decimal("1.4000")
    assert terms.estimated_holdback_pct == Decimal("0.2000")


def test_r42_advance_clamped_to_funder_max() -> None:
    """suggested_max_advance > funder.max_advance → estimated_advance == max."""
    funder = _funder_with_pricing(max_advance=Decimal("100000.00"))
    score = _score(suggested_max_advance=Decimal("500000.00"))
    terms = compute_estimated_terms(funder, score, _deal())
    assert terms is not None
    assert terms.estimated_advance == Decimal("100000.00")


def test_r42_advance_clamped_to_funder_min() -> None:
    """suggested_max_advance < funder.min_advance → estimated_advance == min."""
    funder = _funder_with_pricing(min_advance=Decimal("10000.00"))
    score = _score(suggested_max_advance=Decimal("1000.00"))
    terms = compute_estimated_terms(funder, score, _deal())
    assert terms is not None
    assert terms.estimated_advance == Decimal("10000.00")


def test_r42_apr_returns_positive_decimal() -> None:
    """Synthesized payment stream → calculate_apr returns a positive Decimal."""
    funder = _funder_with_pricing()
    score = _score(
        tier="C",
        suggested_max_advance=Decimal("50000.00"),
        estimated_payback_days=120,
    )
    terms = compute_estimated_terms(funder, score, _deal())
    assert terms is not None
    assert terms.estimated_apr is not None
    assert terms.estimated_apr > Decimal("0")
    # 1.30 factor over 120 days yields APR around 80-100%. Sanity bracket.
    assert terms.estimated_apr < Decimal("2.0")
    # daily payment = 50000 * 1.30 / 120 = 541.67
    assert terms.estimated_daily_payment == Decimal("541.67")


def test_r42_apr_none_on_optimizer_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When calculate_apr raises, estimated_apr is None — no silent 0% fallback."""

    def _always_fail(*_args: object, **_kwargs: object) -> Decimal:
        raise apr_module.APRCalculationError("synthetic failure for test")

    monkeypatch.setattr("aegis.scoring.match_funders.calculate_apr", _always_fail)

    funder = _funder_with_pricing()
    terms = compute_estimated_terms(funder, _score(), _deal())
    assert terms is not None
    assert terms.estimated_apr is None
    # The other fields still populate — APR is the only thing that gates out.
    assert terms.estimated_factor == Decimal("1.3000")


def test_r42_no_pricing_envelope_returns_none() -> None:
    """Funder with no typical_factor/holdback → estimated_terms is None."""
    funder = FunderRow(
        id=uuid4(),
        name="No-pricing Fund",
        min_monthly_revenue=Decimal("25000.00"),
    )
    terms = compute_estimated_terms(funder, _score(), _deal())
    assert terms is None


def test_r42_partial_pricing_envelope_returns_none() -> None:
    """Funder with only factor (no holdback) → None. Won't fabricate the missing end."""
    funder = _funder_with_pricing(typical_holdback_low=None, typical_holdback_high=None)
    terms = compute_estimated_terms(funder, _score(), _deal())
    assert terms is None


def test_r42_no_payback_days_returns_none() -> None:
    """ScoreResult.estimated_payback_days=None → no daily payment → None."""
    funder = _funder_with_pricing()
    terms = compute_estimated_terms(funder, _score(estimated_payback_days=None), _deal())
    assert terms is None


def test_r42_attached_to_funder_match() -> None:
    """End-to-end: match_funder() attaches estimated_terms to the FunderMatch."""
    funder = _funder_with_pricing()
    match = match_funder(funder, _deal(), _score(tier="C"))
    assert match is not None
    assert match.estimated_terms is not None
    assert match.estimated_terms.estimated_factor == Decimal("1.3000")


def test_r42_no_terms_when_funder_lacks_envelope() -> None:
    """match_funder still returns a match, but estimated_terms is None."""
    funder = FunderRow(
        id=uuid4(),
        name="Bare Fund",
        min_monthly_revenue=Decimal("25000.00"),
    )
    match = match_funder(funder, _deal(), _score(tier="C"))
    assert match is not None
    assert match.estimated_terms is None


# ---------------------------------------------------------------------------
# R4.3 — auto_decline + conditional_requirements wiring
# ---------------------------------------------------------------------------


def test_r43_auto_decline_absolute_triggers_hard_fail() -> None:
    """'Do not fund cannabis' contains 'do not fund' → hard fail, verbatim text preserved."""
    funder = FunderRow(
        id=uuid4(),
        name="Strict Fund",
        min_monthly_revenue=Decimal("25000.00"),
        auto_decline_conditions=("Do not fund cannabis",),
    )
    match = match_funder(funder, _deal(industry_naics="111998"), _score())
    assert match is not None
    assert match.match_score == 0, "hard fail must drop likelihood to 0"
    assert any("auto_decline:" in c and "Do not fund cannabis" in c for c in match.soft_concerns)


def test_r43_auto_decline_each_absolute_keyword() -> None:
    """All five absolute triggers route to hard fail."""
    for trigger_text in (
        "Will decline open bankruptcy",
        "Do not fund adult entertainment",
        "Merchant must not have active tax liens",
        "No exception on prior charge-offs",
        "Absolute disqualifier: SBA default",
    ):
        funder = FunderRow(
            id=uuid4(),
            name="Strict",
            min_monthly_revenue=Decimal("25000.00"),
            auto_decline_conditions=(trigger_text,),
        )
        match = match_funder(funder, _deal(), _score())
        assert match is not None, trigger_text
        assert match.match_score == 0, f"{trigger_text!r} did not hard-fail"
        assert any(f"auto_decline: {trigger_text}" in c for c in match.soft_concerns), (
            f"verbatim text missing for {trigger_text!r}"
        )


def test_r43_auto_decline_soft_phrasing_becomes_soft_concern() -> None:
    """'Prefer not to fund liquor stores' has no absolute trigger → soft only."""
    funder = FunderRow(
        id=uuid4(),
        name="Lenient Fund",
        min_monthly_revenue=Decimal("25000.00"),
        auto_decline_conditions=("Prefer not to fund liquor stores",),
    )
    match = match_funder(funder, _deal(), _score(tier="B"))
    assert match is not None
    assert match.match_score > 0, "soft-only entry must not hard-fail"
    assert any(
        "auto_decline_review:" in c and "Prefer not to fund liquor stores" in c
        for c in match.soft_concerns
    )


def test_r43_conditional_requirement_surfaces_verbatim() -> None:
    funder = FunderRow(
        id=uuid4(),
        name="Conditional Fund",
        min_monthly_revenue=Decimal("25000.00"),
        conditional_requirements=("Verify YTD financials",),
    )
    match = match_funder(funder, _deal(), _score(tier="B"))
    assert match is not None
    assert match.match_score > 0
    assert any(
        "conditional:" in c and "Verify YTD financials" in c and "verify with funder" in c
        for c in match.soft_concerns
    )


def test_r43_empty_lists_add_no_concerns() -> None:
    """Both fields empty → matcher behavior unchanged."""
    funder = FunderRow(
        id=uuid4(),
        name="Plain Fund",
        min_monthly_revenue=Decimal("25000.00"),
        auto_decline_conditions=(),
        conditional_requirements=(),
    )
    match = match_funder(funder, _deal(), _score(tier="B"))
    assert match is not None
    assert not any("auto_decline" in c for c in match.soft_concerns)
    assert not any("conditional:" in c for c in match.soft_concerns)


def test_r43_whitespace_only_entries_ignored() -> None:
    """Empty / whitespace-only entries are silently dropped (not surfaced)."""
    funder = FunderRow(
        id=uuid4(),
        name="Sparse Fund",
        min_monthly_revenue=Decimal("25000.00"),
        auto_decline_conditions=("", "   "),
        conditional_requirements=("\t",),
    )
    match = match_funder(funder, _deal(), _score(tier="B"))
    assert match is not None
    assert not any("auto_decline" in c for c in match.soft_concerns)
    assert not any("conditional:" in c for c in match.soft_concerns)


def test_r43_mixed_entries_route_independently() -> None:
    """One absolute auto-decline + one soft + one conditional → all surface as the right kind."""
    funder = FunderRow(
        id=uuid4(),
        name="Mixed Fund",
        min_monthly_revenue=Decimal("25000.00"),
        auto_decline_conditions=(
            "Do not fund cannabis",
            "Prefer to avoid trucking",
        ),
        conditional_requirements=("Trucking: 2 yr MVR clean",),
    )
    match = match_funder(funder, _deal(), _score(tier="B"))
    assert match is not None
    # Hard fail because of the absolute auto_decline.
    assert match.match_score == 0
    concerns = match.soft_concerns
    assert any(c.startswith("auto_decline: ") and "cannabis" in c for c in concerns)
    assert any(c.startswith("auto_decline_review: ") and "trucking" in c.lower() for c in concerns)
    assert any(c.startswith("conditional: ") and "MVR" in c for c in concerns)


# ---------------------------------------------------------------------------
# Offer-sizing wire-through (2026-06-24)
# ---------------------------------------------------------------------------
#
# ``compute_estimated_terms`` + ``evaluate_tier_matches`` accept an optional
# ``offer`` parameter so the funder-match grid seeds from the capacity-
# aware + stack-overload-discounted OfferRecommendation instead of the
# legacy crude ``score.suggested_max_advance`` (tier x revenue multiple).
# These tests pin the four contract points the wire-through guarantees:
#
# 1. ``offer=None`` (the default): behaviour identical to the legacy seed.
# 2. ``offer`` populated: ``estimated_advance`` matches
#    ``offer.recommended_amount`` after the funder-window clamp.
# 3. Offer < funder.min_advance: clamps up to ``min_advance`` (the funder-
#    side floor still binds even when the offer is smaller).
# 4. ``evaluate_tier_matches`` threads the offer the same way — per-tier
#    ``estimated_advance`` seeds from offer when supplied.


def _offer(
    recommended_amount: Decimal,
    *,
    max_amount: Decimal | None = None,
    rationale: str = "sized at 1.0x monthly revenue",
) -> OfferRecommendation:
    """Construct an OfferRecommendation directly (bypass compute_offer)
    so the tests pin the value flow, not the offer math."""
    return OfferRecommendation(
        recommended_amount=recommended_amount,
        max_amount=max_amount if max_amount is not None else recommended_amount,
        holdback_pct=Decimal("0.15"),
        rationale=rationale,
    )


def test_offer_none_keeps_legacy_seed_behaviour_unchanged() -> None:
    """Without offer, ``compute_estimated_terms`` seeds from
    ``score.suggested_max_advance`` exactly as before (regression guard
    for the wire-through's behaviour-neutral default)."""
    funder = _funder_with_pricing()
    score = _score(tier="C", suggested_max_advance=Decimal("75000.00"))
    deal = _deal()
    terms = compute_estimated_terms(funder, score, deal, offer=None)
    assert terms is not None
    # 75k is inside the funder's [5k, 250k] window — passes through.
    assert terms.estimated_advance == Decimal("75000.00")


def test_offer_populated_overrides_legacy_seed_for_advance() -> None:
    """When ``offer.recommended_amount`` differs from
    ``score.suggested_max_advance``, the offer wins — clamped to the
    funder's [min, max] window."""
    funder = _funder_with_pricing()
    score = _score(tier="C", suggested_max_advance=Decimal("75000.00"))
    # Offer is materially different (e.g. capacity-capped at $40k while
    # the legacy crude sizing would have suggested $75k).
    offer = _offer(Decimal("40000.00"))
    terms = compute_estimated_terms(funder, score, _deal(), offer=offer)
    assert terms is not None
    assert terms.estimated_advance == Decimal("40000.00")
    # Confirm the legacy seed was NOT used.
    assert terms.estimated_advance != Decimal("75000.00")


def test_offer_below_funder_min_advance_clamps_up() -> None:
    """Offer below the funder's ``min_advance`` clamps UP to the floor
    — funder-side minimums still bind regardless of which seed fed the
    computation."""
    funder = _funder_with_pricing(min_advance=Decimal("25000.00"))
    score = _score(suggested_max_advance=Decimal("75000.00"))
    offer = _offer(Decimal("8000.00"))  # below the $25k floor
    terms = compute_estimated_terms(funder, score, _deal(), offer=offer)
    assert terms is not None
    assert terms.estimated_advance == Decimal("25000.00")


def test_offer_above_funder_max_advance_clamps_down() -> None:
    """Offer above the funder's ``max_advance`` clamps DOWN to the
    ceiling — symmetric guard so the funder-window clamp logic stays
    intact regardless of seed source."""
    funder = _funder_with_pricing(max_advance=Decimal("100000.00"))
    score = _score(suggested_max_advance=Decimal("50000.00"))
    offer = _offer(Decimal("250000.00"))  # above the $100k ceiling
    terms = compute_estimated_terms(funder, score, _deal(), offer=offer)
    assert terms is not None
    assert terms.estimated_advance == Decimal("100000.00")


def test_evaluate_tier_matches_threads_offer_to_per_tier_advance() -> None:
    """``evaluate_tier_matches`` mirrors ``compute_estimated_terms`` —
    per-tier ``estimated_advance`` is clamped from
    ``offer.recommended_amount`` when supplied, not
    ``score.suggested_max_advance``."""
    from aegis.funders.models import FunderTier

    tier_elite = FunderTier(
        name="Elite",
        min_credit_score=700,
        min_months_in_business=24,
        min_monthly_revenue=Decimal("50000.00"),
        max_positions=0,
        max_advance=Decimal("250000.00"),
        max_holdback=Decimal("0.10"),
        buy_rate_low=Decimal("1.18"),
        buy_rate_high=Decimal("1.22"),
    )
    tier_b = FunderTier(
        name="B",
        min_credit_score=650,
        min_months_in_business=12,
        min_monthly_revenue=Decimal("25000.00"),
        max_positions=1,
        max_advance=Decimal("100000.00"),  # Elite's ceiling cut in half
        max_holdback=Decimal("0.15"),
        buy_rate_low=Decimal("1.25"),
        buy_rate_high=Decimal("1.32"),
    )
    funder = _funder_with_pricing(tiers=(tier_elite, tier_b))
    score = _score(tier="A", suggested_max_advance=Decimal("220000.00"))
    # Offer says only $80k is sustainable (capacity-capped).
    offer = _offer(Decimal("80000.00"))
    tier_matches = evaluate_tier_matches(funder, score, _deal(), offer=offer)

    assert len(tier_matches) == 2
    # Elite's max_advance is $250k — the $80k offer fits under it untouched.
    elite = next(t for t in tier_matches if t.tier_name == "Elite")
    assert elite.estimated_advance == Decimal("80000.00")
    # B's max_advance is $100k — the $80k offer also fits under it.
    b = next(t for t in tier_matches if t.tier_name == "B")
    assert b.estimated_advance == Decimal("80000.00")
    # Confirm the legacy seed ($220k) was NOT used — neither tier should
    # report the score's suggested_max_advance clamped to its ceiling.
    assert elite.estimated_advance != Decimal("220000.00")
    assert b.estimated_advance != Decimal("100000.00")


def test_match_funder_threads_offer_through_to_estimated_terms() -> None:
    """End-to-end: ``match_funder(funder, deal, score, offer=...)`` →
    ``FunderMatch.estimated_terms.estimated_advance`` matches the
    offer-derived value. Pins the public-API contract, not just the
    internal helper."""
    funder = _funder_with_pricing()
    score = _score(tier="C", suggested_max_advance=Decimal("75000.00"))
    offer = _offer(Decimal("40000.00"))
    match = match_funder(funder, _deal(), score, offer=offer)
    assert match is not None
    assert match.estimated_terms is not None
    assert match.estimated_terms.estimated_advance == Decimal("40000.00")
