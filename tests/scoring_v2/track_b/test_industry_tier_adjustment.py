"""Track B — industry tier band adjustment.

Tests the post-cashflow band bump applied by
``apply_industry_tier_adjustment`` plus end-to-end ``compute_risk_band``
behaviour when ``industry_tier`` is threaded through:

* ``standard`` / ``moderate`` -> no change.
* ``elevated``                -> +1 band step.
* ``high_volatility``         -> +2 band steps.
* ``hard_decline_class``      -> force to ``high`` regardless of cash flow.

The adjustment unit tests parametrize across every (cashflow_band,
tier) combination so future band-list changes can't silently shift
the adjustment behaviour. The end-to-end tests stay minimal — build a
synthetic clean bundle, then re-run with each tier and assert the
final band moves the expected number of steps.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from aegis.counterparty.models import CounterpartyClassification
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.industry import IndustryTier
from aegis.scoring_v2.track_b import compute_risk_band
from aegis.scoring_v2.track_b.compute import apply_industry_tier_adjustment
from aegis.scoring_v2.track_b.models import BandLevel

# ─────────────────────────────────────────────────────────────────────
# apply_industry_tier_adjustment — unit
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cashflow_band", ["low", "moderate", "elevated", "high"])
@pytest.mark.parametrize("neutral_tier", ["standard", "moderate"])
def test_standard_and_moderate_do_not_adjust(
    cashflow_band: BandLevel, neutral_tier: IndustryTier
) -> None:
    """``standard`` and ``moderate`` are pass-through tiers — they
    document the merchant's industry on the reasons list without
    influencing the final band."""
    assert apply_industry_tier_adjustment(cashflow_band, neutral_tier) == cashflow_band


@pytest.mark.parametrize(
    ("cashflow_band", "expected"),
    [
        ("low", "moderate"),
        ("moderate", "elevated"),
        ("elevated", "high"),
        ("high", "high"),  # clamped — no level beyond high
    ],
)
def test_elevated_tier_bumps_one_step_capped_at_high(
    cashflow_band: BandLevel, expected: BandLevel
) -> None:
    assert apply_industry_tier_adjustment(cashflow_band, "elevated") == expected


@pytest.mark.parametrize(
    ("cashflow_band", "expected"),
    [
        ("low", "elevated"),
        ("moderate", "high"),
        ("elevated", "high"),  # clamped
        ("high", "high"),
    ],
)
def test_high_volatility_tier_bumps_two_steps_capped_at_high(
    cashflow_band: BandLevel, expected: BandLevel
) -> None:
    assert apply_industry_tier_adjustment(cashflow_band, "high_volatility") == expected


@pytest.mark.parametrize("cashflow_band", ["low", "moderate", "elevated", "high"])
def test_hard_decline_class_forces_high(cashflow_band: BandLevel) -> None:
    """The spec line: 'hard_decline_class forces band to high regardless
    of cash flow'. Even a ``low`` cashflow band becomes ``high``."""
    assert apply_industry_tier_adjustment(cashflow_band, "hard_decline_class") == "high"


# ─────────────────────────────────────────────────────────────────────
# compute_risk_band — end-to-end with synthetic bundle
# ─────────────────────────────────────────────────────────────────────


def _synthetic_clean_bundle() -> tuple[
    dict[str, list[ClassifiedTransaction]],
    dict[UUID, CounterpartyClassification],
]:
    """Minimal bundle that produces a ``low`` cashflow band: one
    healthy revenue deposit, no NSFs, no MCA debits. The
    counterparty classifier sees the deposit as ``end_customer``
    revenue so the aggregation gives a real ``revenue_total``."""
    deposit = ClassifiedTransaction(
        id=uuid4(),
        posted_date=date(2026, 2, 14),
        description="ACME CUSTOMER WIRE",
        amount=Decimal("60000.00"),
        running_balance=Decimal("60000.00"),
        source_page=1,
        source_line=1,
        category="wire_in",
        classification_confidence=98,
    )
    transactions_by_doc = {"doc_1": [deposit]}
    classifications: dict[UUID, CounterpartyClassification] = {
        deposit.id: CounterpartyClassification(
            transaction_id=deposit.id,
            counterparty="end_customer",
            confidence=98,
            reason="customer wire",
        ),
    }
    return transactions_by_doc, classifications


def test_compute_risk_band_no_industry_tier_keeps_cashflow_band() -> None:
    """Baseline — no industry threading -> compute_risk_band returns
    the cashflow-derived band unchanged. The reasons list does NOT
    include an ``industry_tier`` row."""
    txns, classifs = _synthetic_clean_bundle()
    result = compute_risk_band(txns, classifs)
    assert result.band == "low"
    assert all(r.factor != "industry_tier" for r in result.reasons)


def test_compute_risk_band_standard_industry_no_adjustment_has_reason_row() -> None:
    """``standard`` doesn't bump the band but DOES emit a reasons row
    so the underwriter sees that the industry was considered."""
    txns, classifs = _synthetic_clean_bundle()
    result = compute_risk_band(txns, classifs, industry_tier="standard")
    assert result.band == "low"  # unchanged
    industry_reasons = [r for r in result.reasons if r.factor == "industry_tier"]
    assert len(industry_reasons) == 1
    assert industry_reasons[0].severity == "positive"


def test_compute_risk_band_elevated_industry_bumps_one_step() -> None:
    """Cashflow says ``low``, industry tier is ``elevated`` -> band
    becomes ``moderate``."""
    txns, classifs = _synthetic_clean_bundle()
    result = compute_risk_band(txns, classifs, industry_tier="elevated")
    assert result.band == "moderate"
    industry_reasons = [r for r in result.reasons if r.factor == "industry_tier"]
    assert len(industry_reasons) == 1
    assert industry_reasons[0].severity == "elevated"


def test_compute_risk_band_high_volatility_bumps_two_steps() -> None:
    """Cashflow ``low`` + ``high_volatility`` industry -> ``elevated``."""
    txns, classifs = _synthetic_clean_bundle()
    result = compute_risk_band(txns, classifs, industry_tier="high_volatility")
    assert result.band == "elevated"


def test_compute_risk_band_hard_decline_class_forces_high() -> None:
    """The spec: ``hard_decline_class`` forces the band to ``high``
    regardless of cash flow. End-to-end check — even a pristine
    ``low`` cashflow ends up ``high``."""
    txns, classifs = _synthetic_clean_bundle()
    result = compute_risk_band(txns, classifs, industry_tier="hard_decline_class")
    assert result.band == "high"
    # The action mapping for ``high`` is ``review_decline_default`` —
    # the informational equivalent of the live decline path.
    assert result.action == "review_decline_default"
    industry_reasons = [r for r in result.reasons if r.factor == "industry_tier"]
    assert len(industry_reasons) == 1
    assert industry_reasons[0].severity == "critical"


def test_industry_tier_reason_text_includes_tier_name() -> None:
    """Detail string carries the tier name so the dossier chip's
    qualifier line + the reasons-list entry stay readable without
    extra lookup."""
    txns, classifs = _synthetic_clean_bundle()
    result = compute_risk_band(txns, classifs, industry_tier="high_volatility")
    industry_reasons = [r for r in result.reasons if r.factor == "industry_tier"]
    assert "high_volatility" in industry_reasons[0].detail
