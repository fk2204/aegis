"""Tests for ``aegis.scoring_v2.industry``.

Covers the pure lookup function. Track B integration of the tier is
exercised separately in ``test_track_b_industry_adjustment.py``.
"""

from __future__ import annotations

import pytest

from aegis.scoring_v2.industry import (
    INDUSTRY_RISK_TIERS,
    UNKNOWN_INDUSTRY_TIER,
    IndustryTier,
    industry_risk_tier,
    industry_tier_reason,
)

# ─────────────────────────────────────────────────────────────────────
# Known classifications
# ─────────────────────────────────────────────────────────────────────


def test_known_high_volatility_industry() -> None:
    """Restaurant / Food Service is one of the canonical high-volatility
    classifications — seasonal demand, margin-thin operating model."""
    assert industry_risk_tier("Restaurant / Food Service") == "high_volatility"


def test_known_standard_industry() -> None:
    """Healthcare practices are stable, recession-resistant — standard tier."""
    assert industry_risk_tier("Healthcare — Dental") == "standard"
    assert industry_risk_tier("Healthcare — Medical Practice") == "standard"
    assert industry_risk_tier("Healthcare — Veterinary") == "standard"


def test_known_elevated_industry() -> None:
    """Auto Repair / Service — above-baseline (asset-intensive, single-
    revenue-source). Elevated tier."""
    assert industry_risk_tier("Auto Repair / Service") == "elevated"
    assert industry_risk_tier("Manufacturing") == "elevated"


def test_known_moderate_industry() -> None:
    """Professional Services is explicitly classified as moderate (not
    just defaulted) so its tier doesn't drift if UNKNOWN_INDUSTRY_TIER
    changes."""
    assert industry_risk_tier("Professional Services") == "moderate"
    assert industry_risk_tier("Wholesale / Distribution") == "moderate"


# ─────────────────────────────────────────────────────────────────────
# Unknown / missing -> moderate (conservative default)
# ─────────────────────────────────────────────────────────────────────


def test_unknown_industry_defaults_to_moderate() -> None:
    """Spec: 'unknown industry returns moderate (safe default, not
    permissive)'. An industry the operator hasn't classified must not
    silently inherit ``standard``."""
    assert industry_risk_tier("Quantum Computing") == "moderate"
    assert industry_risk_tier("RANDOM_STRING_NOT_IN_CLOSE") == UNKNOWN_INDUSTRY_TIER
    assert UNKNOWN_INDUSTRY_TIER == "moderate"


def test_none_input_defaults_to_moderate() -> None:
    """``None`` means the merchant has no industry on file yet."""
    assert industry_risk_tier(None) == "moderate"


def test_empty_string_defaults_to_moderate() -> None:
    assert industry_risk_tier("") == "moderate"
    assert industry_risk_tier("   ") == "moderate"


def test_close_none_sentinel_defaults_to_moderate() -> None:
    """Close's ``-None-`` choice-field sentinel folds to moderate."""
    assert industry_risk_tier("-None-") == "moderate"


# ─────────────────────────────────────────────────────────────────────
# Em-dash / hyphen normalization
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("input_str", "expected_tier"),
    [
        # Opportunity-side hyphen form -> normalized to em-dash form ->
        # canonical Lead-side key.
        ("Construction - General Contractor", "high_volatility"),
        ("Construction - Specialty Trades", "high_volatility"),
        ("Healthcare - Dental", "standard"),
        ("Healthcare - Medical Practice", "standard"),
        ("Retail - General", "elevated"),
        ("Retail - Specialty", "elevated"),
    ],
)
def test_hyphen_input_normalizes_to_em_dash_key(
    input_str: str, expected_tier: IndustryTier
) -> None:
    """Opportunity-side ``Industry`` strings use regular hyphens; the
    function normalizes ``" - "`` -> ``" — "`` before lookup so both
    Close fields resolve to the same tier."""
    assert industry_risk_tier(input_str) == expected_tier


def test_em_dash_input_unchanged() -> None:
    """Lead-side em-dash form is the canonical key. No transformation."""
    assert industry_risk_tier("Construction — General Contractor") == "high_volatility"


def test_whitespace_stripped_before_lookup() -> None:
    assert industry_risk_tier("  Manufacturing  ") == "elevated"


# ─────────────────────────────────────────────────────────────────────
# Structural guards
# ─────────────────────────────────────────────────────────────────────


def test_every_known_industry_has_a_reason() -> None:
    """Every tier in :data:`INDUSTRY_RISK_TIERS`'s value set has a
    corresponding entry in :data:`INDUSTRY_TIER_REASONS`. A new tier
    that lacks a reason would break the dossier chip qualifier."""
    used_tiers = set(INDUSTRY_RISK_TIERS.values())
    for tier in used_tiers:
        assert industry_tier_reason(tier), f"no reason for tier {tier!r}"


def test_unknown_tier_has_a_reason() -> None:
    """The default tier's reason is what the chip surfaces for
    unclassified merchants — must be non-empty."""
    assert industry_tier_reason(UNKNOWN_INDUSTRY_TIER)


def test_lookup_table_contains_only_known_tiers() -> None:
    """Guard against typos in the lookup table values."""
    allowed = {
        "standard",
        "moderate",
        "elevated",
        "high_volatility",
        "hard_decline_class",
    }
    for industry, tier in INDUSTRY_RISK_TIERS.items():
        assert tier in allowed, f"{industry!r} -> {tier!r} is not a valid IndustryTier"
