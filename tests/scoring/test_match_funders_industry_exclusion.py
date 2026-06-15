"""Industry-exclusion gate in ``match_funder`` — word-set match.

The pre-fix gate compared lowercased ``excluded_industries`` tokens
(``"restaurant"``, ``"trucking"``) against the merchant's
``industry_naics`` (``"722511"``, ``"484110"``). The two sides never
matched, so a funder with ``excluded_industries=("restaurant",)``
silently approved a restaurant merchant.

Post-fix the gate compares against the merchant's
``industry_choice`` string (Close Lead-side em-dash form, e.g.
``"Restaurant / Food Service"``) using a word-set match. Falls back
to ``CLOSE_INDUSTRY_TO_NAICS`` reverse lookup when the merchant has
no ``industry_choice`` on file.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _deal(**overrides: object) -> ScoreInput:
    """Baseline deal — qualifies on every other gate; only the
    industry-exclusion gate varies per test."""
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Restaurant",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="722511",
        industry_choice="Restaurant / Food Service",
        time_in_business_months=36,
        credit_score=720,
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


def _funder(**overrides: object) -> FunderRow:
    """Minimal funder with one always-published criterion (so empty-
    exclusions cases still return a FunderMatch instead of None — the
    matcher returns None when criteria_count == 0)."""
    base = FunderRow(name="Test Funder", min_credit_score=600)
    return base.model_copy(update=overrides)


# ─────────────────────────────────────────────────────────────────────
# Headline test — the bug
# ─────────────────────────────────────────────────────────────────────


def test_restaurant_token_excludes_restaurant_merchant() -> None:
    """The pre-fix matcher silently approved this. Now it must hard fail."""
    funder = _funder(excluded_industries=("restaurant",))
    deal = _deal()  # industry_choice="Restaurant / Food Service"
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 0
    assert any("industry_excluded" in c for c in match.soft_concerns)
    assert any("restaurant" in c for c in match.soft_concerns)


# ─────────────────────────────────────────────────────────────────────
# Non-matching industries should NOT trigger the exclusion
# ─────────────────────────────────────────────────────────────────────


def test_restaurant_token_does_not_exclude_auto_repair_merchant() -> None:
    funder = _funder(excluded_industries=("restaurant",))
    deal = _deal(
        industry_choice="Auto Repair / Service",
        industry_naics="811111",
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score > 0
    assert not any("industry_excluded" in c for c in match.soft_concerns)


def test_trucking_token_does_not_exclude_restaurant_merchant() -> None:
    """Word-set match guards against partial-substring confusion."""
    funder = _funder(excluded_industries=("trucking",))
    deal = _deal()  # restaurant
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("industry_excluded" in c for c in match.soft_concerns)


# ─────────────────────────────────────────────────────────────────────
# Hyphenated tokens still match against slash-separated choice strings
# ─────────────────────────────────────────────────────────────────────


def test_hyphenated_token_matches_when_all_words_present() -> None:
    """``"adult-entertainment"`` tokenises to ``{adult, entertainment}``
    which is a subset of a hypothetical ``"Adult / Entertainment"`` choice.
    Real Close dropdown doesn't list this industry today but the gate
    must still resolve hyphenated extractor tokens correctly when one
    is added (or operator-typed)."""
    funder = _funder(excluded_industries=("adult-entertainment",))
    deal = _deal(
        industry_choice="Adult / Entertainment",
        industry_naics=None,
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert any("industry_excluded" in c and "adult-entertainment" in c for c in match.soft_concerns)


def test_token_with_extra_words_does_not_match() -> None:
    """``"trucking-logistics"`` tokenises to ``{trucking, logistics}``
    which IS a subset of ``"Trucking / Logistics"`` — so it matches.
    But ``"trucking-logistics-fleet"`` adds ``"fleet"`` which the choice
    doesn't contain → subset check fails → no match. Guards against
    over-broad exclusion tokens silently picking up unrelated
    merchants."""
    funder = _funder(excluded_industries=("trucking-logistics-fleet",))
    deal = _deal(
        industry_choice="Trucking / Logistics",
        industry_naics="484110",
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("industry_excluded" in c for c in match.soft_concerns)


# ─────────────────────────────────────────────────────────────────────
# NAICS-derived fallback when industry_choice is missing
# ─────────────────────────────────────────────────────────────────────


def test_naics_fallback_when_industry_choice_missing() -> None:
    """Legacy merchant pre-migration 055: ``industry_choice=None`` but
    ``industry_naics`` is set. The gate reverse-looks-up the NAICS
    code via ``CLOSE_INDUSTRY_TO_NAICS`` and matches against that
    derived name. ``722511`` -> ``"Restaurant / Food Service"`` ->
    matches ``"restaurant"``."""
    funder = _funder(excluded_industries=("restaurant",))
    deal = _deal(
        industry_choice=None,
        industry_naics="722511",
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert any("industry_excluded" in c for c in match.soft_concerns)


def test_naics_fallback_misses_when_code_not_in_table() -> None:
    """Operator-typed NAICS that isn't in ``CLOSE_INDUSTRY_TO_NAICS``
    (e.g. ``"311111"`` for food manufacturing — not in the broker's
    Industry dropdown). No fallback name available → no match → no
    exclusion fires. The deal qualifies; operator can edit the funder
    or merchant to surface the gap."""
    funder = _funder(excluded_industries=("cannabis",))
    deal = _deal(
        industry_choice=None,
        industry_naics="311111",  # not in CLOSE_INDUSTRY_TO_NAICS
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("industry_excluded" in c for c in match.soft_concerns)


def test_both_industry_sources_missing_skips_gate() -> None:
    """No ``industry_choice``, no ``industry_naics`` — the gate has
    nothing to compare against and must NOT fire. The exclusion list
    still contributes to ``criteria_count`` (the funder published
    one, even if we can't evaluate it) so a downstream consumer
    can tell."""
    funder = _funder(excluded_industries=("restaurant",))
    deal = _deal(
        industry_choice=None,
        industry_naics=None,
    )
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("industry_excluded" in c for c in match.soft_concerns)


# ─────────────────────────────────────────────────────────────────────
# Multiple exclusion tokens
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("excluded", "industry_choice", "should_fire"),
    [
        # Multiple tokens, one matches.
        (("trucking", "restaurant", "cannabis"), "Restaurant / Food Service", True),
        # Multiple tokens, none matches.
        (("trucking", "cannabis"), "Restaurant / Food Service", False),
        # Empty tokens list.
        ((), "Restaurant / Food Service", False),
        # Whitespace token shouldn't match anything.
        (("   ",), "Restaurant / Food Service", False),
    ],
)
def test_multi_token_exclusion(
    excluded: tuple[str, ...], industry_choice: str, should_fire: bool
) -> None:
    funder = _funder(excluded_industries=excluded)
    deal = _deal(industry_choice=industry_choice)
    match = match_funder(funder, deal, _score())
    assert match is not None
    fired = any("industry_excluded" in c for c in match.soft_concerns)
    assert fired is should_fire
