"""U28 — tier-aware matching tests (SHADOW MODE).

Verifies that ``evaluate_tier_matches`` reads ``FunderRow.tiers`` and emits
a per-tier ``TierMatch`` signal on ``FunderMatch.tier_matches``, AND that
the new signal does NOT alter ``match_score``, ``soft_concerns``, or
``reasons`` (shadow discipline per CLAUDE.md "Decision-boundary changes —
shadow-first").

Test fixtures model two real funders the operator has curated:

* Logic Advance — 4-tier ladder (Elite / Premium / Standard / High-Risk).
  A high-FICO merchant should qualify for every tier; a low-FICO merchant
  should only clear the High-Risk floor.
* United Capital Source — 7 "tiers" representing product lines (MCA,
  Term Loan, SBA Express, …). An MCA-eligible merchant qualifies for the
  MCA line; SBA / equipment-finance lines have higher floors the same
  merchant fails.

A control funder (Splash Advance — no tiers populated) confirms
``tier_matches`` is the empty list, not None.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from aegis.funders.models import FunderRow, FunderTier
from aegis.scoring.match_funders import evaluate_tier_matches, match_funder
from aegis.scoring.models import ScoreInput, ScoreResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _deal(**overrides: object) -> ScoreInput:
    """Baseline ScoreInput; overrides one field at a time per test."""
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="238320",
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
    tier: Literal["A", "B", "C", "D", "F"] = "B",
    suggested_max_advance: Decimal = Decimal("100000.00"),
) -> ScoreResult:
    return ScoreResult(
        score=70,
        tier=tier,
        recommendation="approve",
        suggested_max_advance=suggested_max_advance,
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def _logic_advance_funder() -> FunderRow:
    """Four-tier underwriting ladder, most-permissive → most-restrictive.

    Real operator-curated thresholds from migration 046. Elite is the
    tightest gate (FICO 720+, 36mo TIB, 1st-position, $40k/mo revenue);
    High-Risk is the floor (FICO 550, 6mo TIB, 3-position, $20k/mo).
    """
    return FunderRow(
        id=uuid4(),
        name="Logic Advance",
        active=True,
        # Top-level mirrors the loosest tier (High-Risk) — that's the bug
        # this U28 work compensates for: top-level fields under-state the
        # actual matrix when only the loosest tier is reflected.
        min_credit_score=550,
        min_months_in_business=6,
        min_monthly_revenue=Decimal("20000.00"),
        max_positions=3,
        typical_factor_low=Decimal("1.18"),
        typical_factor_high=Decimal("1.45"),
        typical_holdback_low=Decimal("0.10"),
        typical_holdback_high=Decimal("0.22"),
        max_advance=Decimal("250000.00"),
        tiers=(
            FunderTier(
                name="Elite",
                buy_rate_low=Decimal("1.18"),
                buy_rate_high=Decimal("1.22"),
                min_credit_score=720,
                min_months_in_business=36,
                min_monthly_revenue=Decimal("40000.00"),
                max_positions=0,
                max_advance=Decimal("250000.00"),
                max_holdback=Decimal("0.10"),
            ),
            FunderTier(
                name="Premium",
                buy_rate_low=Decimal("1.24"),
                buy_rate_high=Decimal("1.30"),
                min_credit_score=680,
                min_months_in_business=24,
                min_monthly_revenue=Decimal("30000.00"),
                max_positions=1,
                max_advance=Decimal("200000.00"),
                max_holdback=Decimal("0.13"),
            ),
            FunderTier(
                name="Standard",
                buy_rate_low=Decimal("1.32"),
                buy_rate_high=Decimal("1.38"),
                min_credit_score=620,
                min_months_in_business=12,
                min_monthly_revenue=Decimal("25000.00"),
                max_positions=2,
                max_advance=Decimal("100000.00"),
                max_holdback=Decimal("0.18"),
            ),
            FunderTier(
                name="High-Risk",
                buy_rate_low=Decimal("1.40"),
                buy_rate_high=Decimal("1.45"),
                min_credit_score=550,
                min_months_in_business=6,
                min_monthly_revenue=Decimal("20000.00"),
                max_positions=3,
                max_advance=Decimal("60000.00"),
                max_holdback=Decimal("0.22"),
            ),
        ),
    )


def _ucs_funder() -> FunderRow:
    """United Capital Source's 7 product lines modelled as tiers.

    Each "tier" is a distinct product (MCA, Term, SBA Express, …) with
    its own floor — operator's pragmatic use of the tiers structure to
    represent product-line eligibility rather than tier-of-the-same-
    product.
    """
    return FunderRow(
        id=uuid4(),
        name="United Capital Source",
        active=True,
        min_credit_score=500,
        min_months_in_business=4,
        min_monthly_revenue=Decimal("15000.00"),
        max_positions=4,
        tiers=(
            FunderTier(
                name="MCA",
                buy_rate_low=Decimal("1.20"),
                buy_rate_high=Decimal("1.49"),
                min_credit_score=500,
                min_months_in_business=4,
                min_monthly_revenue=Decimal("15000.00"),
                max_positions=4,
                max_advance=Decimal("500000.00"),
                max_holdback=Decimal("0.25"),
            ),
            FunderTier(
                name="Term Loan",
                buy_rate_low=Decimal("1.10"),
                buy_rate_high=Decimal("1.25"),
                min_credit_score=650,
                min_months_in_business=24,
                min_monthly_revenue=Decimal("30000.00"),
                max_positions=1,
                max_advance=Decimal("1000000.00"),
            ),
            FunderTier(
                name="SBA Express",
                min_credit_score=680,
                min_months_in_business=24,
                min_monthly_revenue=Decimal("40000.00"),
                max_positions=0,
                max_advance=Decimal("500000.00"),
            ),
            FunderTier(
                name="Equipment Financing",
                min_credit_score=620,
                min_months_in_business=12,
                min_monthly_revenue=Decimal("25000.00"),
                max_advance=Decimal("2000000.00"),
            ),
            FunderTier(
                name="Invoice Factoring",
                min_credit_score=550,
                min_months_in_business=6,
                min_monthly_revenue=Decimal("20000.00"),
            ),
            FunderTier(
                name="Business Line of Credit",
                min_credit_score=660,
                min_months_in_business=12,
                min_monthly_revenue=Decimal("25000.00"),
                max_positions=2,
                max_advance=Decimal("250000.00"),
            ),
            FunderTier(
                name="Revenue Based Financing",
                buy_rate_low=Decimal("1.15"),
                buy_rate_high=Decimal("1.35"),
                min_credit_score=600,
                min_months_in_business=12,
                min_monthly_revenue=Decimal("20000.00"),
                max_positions=3,
                max_advance=Decimal("750000.00"),
            ),
        ),
    )


def _splash_no_tiers() -> FunderRow:
    """Control funder — top-level criteria but no structured tiers."""
    return FunderRow(
        id=uuid4(),
        name="Splash Advance",
        active=True,
        min_credit_score=600,
        min_months_in_business=12,
        min_monthly_revenue=Decimal("25000.00"),
        max_positions=2,
        tiers=(),
    )


# ---------------------------------------------------------------------------
# Logic Advance — 4-tier ladder
# ---------------------------------------------------------------------------


def test_logic_advance_strong_merchant_qualifies_for_all_four_tiers() -> None:
    """FICO 760 / 60mo TIB / $80k revenue / 1st-position → clears every tier."""
    funder = _logic_advance_funder()
    deal = _deal(
        credit_score=760,
        time_in_business_months=60,
        monthly_revenue=Decimal("80000.00"),
        mca_positions=0,
    )
    matches = evaluate_tier_matches(funder, _score(tier="A"), deal)
    assert len(matches) == 4
    assert [m.tier_name for m in matches] == [
        "Elite",
        "Premium",
        "Standard",
        "High-Risk",
    ]
    assert all(m.qualifies for m in matches)
    assert all(m.disqualifying_reasons == [] for m in matches)

    elite = matches[0]
    assert elite.qualifies is True
    # Elite publishes the lowest buy-rate envelope.
    assert elite.estimated_factor_low == Decimal("1.18")
    assert elite.estimated_factor_high == Decimal("1.22")
    assert elite.estimated_holdback == Decimal("0.10")
    # suggested_max_advance ($100k) < Elite.max_advance ($250k) → uncapped.
    assert elite.estimated_advance == Decimal("100000.00")


def test_logic_advance_weak_merchant_only_qualifies_for_high_risk() -> None:
    """FICO 580 / 8mo TIB / $22k revenue / 2-position → only clears High-Risk."""
    funder = _logic_advance_funder()
    deal = _deal(
        credit_score=580,
        time_in_business_months=8,
        monthly_revenue=Decimal("22000.00"),
        mca_positions=2,
    )
    matches = evaluate_tier_matches(funder, _score(tier="D"), deal)
    by_name = {m.tier_name: m for m in matches}

    assert by_name["Elite"].qualifies is False
    assert by_name["Premium"].qualifies is False
    assert by_name["Standard"].qualifies is False
    assert by_name["High-Risk"].qualifies is True
    assert by_name["High-Risk"].disqualifying_reasons == []

    # Elite fails on credit AND tib AND revenue AND positions — verify the
    # full set of axis reasons surfaces, not just the first.
    elite_reasons = by_name["Elite"].disqualifying_reasons
    assert any("credit 580 < min 720" in r for r in elite_reasons)
    assert any("tib 8mo < min 36mo" in r for r in elite_reasons)
    assert any("min $40000.00" in r for r in elite_reasons)
    assert any("positions 2 > max 0" in r for r in elite_reasons)


def test_logic_advance_best_fit_economics_attached_to_match() -> None:
    """match_funder() wires tier_matches into FunderMatch; strong merchant
    sees the Elite tier with its low buy-rate envelope alongside the
    legacy ``estimated_terms``."""
    funder = _logic_advance_funder()
    deal = _deal(credit_score=760, time_in_business_months=60)
    match = match_funder(funder, deal, _score(tier="A"))
    assert match is not None

    qualifying = [m for m in match.tier_matches if m.qualifies]
    assert len(qualifying) == 4
    # Lowest factor among qualifying tiers should be Elite's 1.18.
    best = min(
        (m for m in qualifying if m.estimated_factor_low is not None),
        key=lambda m: m.estimated_factor_low,  # type: ignore[arg-type,return-value]
    )
    assert best.tier_name == "Elite"
    assert best.estimated_factor_low == Decimal("1.18")


# ---------------------------------------------------------------------------
# UCS — 7 product lines as tiers
# ---------------------------------------------------------------------------


def test_ucs_mca_eligible_merchant_clears_mca_tier() -> None:
    """MCA-shaped merchant (mid FICO / 12mo TIB / $30k rev / 2-position)
    qualifies for the MCA line but fails SBA / Term Loan thresholds."""
    funder = _ucs_funder()
    deal = _deal(
        credit_score=620,
        time_in_business_months=12,
        monthly_revenue=Decimal("30000.00"),
        mca_positions=2,
    )
    matches = evaluate_tier_matches(funder, _score(tier="C"), deal)
    by_name = {m.tier_name: m for m in matches}

    assert len(matches) == 7
    assert by_name["MCA"].qualifies is True
    assert by_name["MCA"].disqualifying_reasons == []
    # MCA economics surface from the tier, not the funder-level envelope.
    assert by_name["MCA"].estimated_factor_low == Decimal("1.20")
    assert by_name["MCA"].estimated_factor_high == Decimal("1.49")
    assert by_name["MCA"].estimated_holdback == Decimal("0.25")

    # Term Loan and SBA Express both demand FICO 650+ and 24mo TIB.
    assert by_name["Term Loan"].qualifies is False
    assert by_name["SBA Express"].qualifies is False


def test_ucs_tier_without_advance_ceiling_emits_none() -> None:
    """Invoice Factoring has no max_advance → estimated_advance is None."""
    funder = _ucs_funder()
    matches = evaluate_tier_matches(funder, _score(), _deal())
    invoice = next(m for m in matches if m.tier_name == "Invoice Factoring")
    assert invoice.estimated_advance is None
    assert invoice.estimated_factor_low is None  # tier omits buy_rate too
    assert invoice.estimated_factor_high is None


# ---------------------------------------------------------------------------
# Control: funder with no tiers
# ---------------------------------------------------------------------------


def test_funder_without_tiers_has_empty_tier_matches() -> None:
    """Splash Advance has tiers=() → tier_matches is the empty list."""
    funder = _splash_no_tiers()
    match = match_funder(funder, _deal(), _score(tier="B"))
    assert match is not None
    assert match.tier_matches == []


def test_evaluate_tier_matches_helper_returns_empty_for_no_tiers() -> None:
    """Direct helper call mirrors the match_funder wiring."""
    funder = _splash_no_tiers()
    assert evaluate_tier_matches(funder, _score(), _deal()) == []


# ---------------------------------------------------------------------------
# Shadow discipline
# ---------------------------------------------------------------------------


def test_shadow_match_score_unchanged_regardless_of_tier_matches() -> None:
    """Whether a merchant qualifies for 0 tiers or 4, FunderMatch.match_score
    matches what the funder-level criteria would produce — tier_matches is
    annotation-only."""
    funder = _logic_advance_funder()

    strong = _deal(
        credit_score=760, time_in_business_months=60, monthly_revenue=Decimal("80000.00")
    )
    weak = _deal(credit_score=580, time_in_business_months=8, monthly_revenue=Decimal("22000.00"))

    # Strong: clears every funder-level gate AND every tier.
    strong_match = match_funder(funder, strong, _score(tier="A"))
    assert strong_match is not None
    assert all(m.qualifies for m in strong_match.tier_matches)

    # Weak: still clears the funder-level gates (which mirror the loosest
    # tier) so match_score stays positive. Tier matrix tells the other
    # story — Elite/Premium/Standard all fail.
    weak_match = match_funder(funder, weak, _score(tier="A"))
    assert weak_match is not None
    assert weak_match.match_score > 0, (
        "shadow regression: weak merchant got hard-failed at the funder "
        "level because of tier_matches"
    )
    assert sum(1 for m in weak_match.tier_matches if not m.qualifies) == 3


def test_shadow_disqualifying_tiers_do_not_pollute_soft_concerns() -> None:
    """Per-tier disqualifying_reasons must NOT leak into
    FunderMatch.soft_concerns. The whole point of shadow mode is keeping
    these separate until the operator validates."""
    funder = _logic_advance_funder()
    weak = _deal(credit_score=580, time_in_business_months=8, monthly_revenue=Decimal("22000.00"))
    match = match_funder(funder, weak, _score(tier="A"))
    assert match is not None

    # Collect every per-tier reason string from the disqualified tiers.
    tier_reasons: set[str] = set()
    for tm in match.tier_matches:
        tier_reasons.update(tm.disqualifying_reasons)
    assert tier_reasons, "expected at least one disqualified tier in this fixture"

    for reason in tier_reasons:
        assert reason not in match.soft_concerns, (
            f"shadow leak: tier reason {reason!r} surfaced in soft_concerns"
        )


def test_reasons_includes_qualifying_tier_name_now_that_matrix_is_live() -> None:
    """Post-commit ``5a9b85a + tier-matrix-live``: when a funder has
    tiers, the qualifying tier name lands in ``reasons`` ahead of the
    score-tier marker. The dossier reads "Qualifies at Tier Elite"
    instead of just "tier_B"."""
    funder = _logic_advance_funder()
    deal = _deal()
    match = match_funder(funder, deal, _score(tier="B"))
    assert match is not None
    # One qualifies_at_tier entry + the score-tier marker.
    qualifies_entries = [r for r in match.reasons if r.startswith("qualifies_at_tier:")]
    assert len(qualifies_entries) == 1
    assert "tier_B" in match.reasons
