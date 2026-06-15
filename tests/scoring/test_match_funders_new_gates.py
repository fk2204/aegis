"""Three new funder gates from migration 056:

* ``deal_types_accepted`` — hard fail when funder publishes a product
  list and the deal's ``deal_type`` isn't on it.
* ``funding_velocity_days`` — soft concern when the merchant's Close
  Urgency starts with ``"ASAP"`` and the funder takes longer than 2
  business days.
* ``preferred_states`` — soft concern when funder published a state
  preference and the deal's state isn't on the list. Distinct from
  ``excluded_states`` which hard-fails.
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
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="811111",
        industry_choice="Auto Repair / Service",
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
        deal_type="mca",
        urgency=None,
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
    base = FunderRow(name="Gate Test Funder", min_credit_score=600)
    return base.model_copy(update=overrides)


# ─────────────────────────────────────────────────────────────────────
# deal_types_accepted — hard fail gate
# ─────────────────────────────────────────────────────────────────────


def test_deal_type_in_accepted_list_passes() -> None:
    """Funder writes MCA + LOC + Term; deal is MCA -> no fail."""
    funder = _funder(deal_types_accepted=("mca", "loc", "term_loan"))
    deal = _deal(deal_type="mca")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("deal_type_not_accepted" in c for c in match.soft_concerns)


def test_deal_type_not_in_list_hard_fails() -> None:
    """Funder writes LOC only; deal is MCA -> hard fail."""
    funder = _funder(deal_types_accepted=("loc",))
    deal = _deal(deal_type="mca")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score == 0
    assert any("deal_type_not_accepted" in c for c in match.soft_concerns)


def test_empty_deal_types_skips_gate() -> None:
    """Funder hasn't published a deal-type policy (empty tuple) ->
    every deal type passes."""
    funder = _funder(deal_types_accepted=())
    deal = _deal(deal_type="anything_goes")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("deal_type_not_accepted" in c for c in match.soft_concerns)


def test_deal_type_match_is_case_insensitive() -> None:
    funder = _funder(deal_types_accepted=("MCA", "LOC"))
    deal = _deal(deal_type="mca")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("deal_type_not_accepted" in c for c in match.soft_concerns)


# ─────────────────────────────────────────────────────────────────────
# funding_velocity_days — soft concern when ASAP + slow
# ─────────────────────────────────────────────────────────────────────


def test_slow_funder_with_asap_merchant_fires_soft_concern() -> None:
    """Funder takes 5 business days; merchant urgency is ASAP -> soft
    concern. Deal still qualifies (no hard fail, match_score > 0)."""
    funder = _funder(funding_velocity_days=5)
    deal = _deal(urgency="ASAP (24-48 hours)")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score > 0
    matched = [c for c in match.soft_concerns if "funding_velocity_mismatch" in c]
    assert len(matched) == 1
    assert "5_business_days" in matched[0]


def test_fast_funder_with_asap_merchant_no_concern() -> None:
    """Funder takes 1 business day; merchant urgency is ASAP -> no
    velocity concern (1 day fits the ASAP window)."""
    funder = _funder(funding_velocity_days=1)
    deal = _deal(urgency="ASAP (24-48 hours)")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("funding_velocity_mismatch" in c for c in match.soft_concerns)


def test_2_day_funder_with_asap_merchant_at_threshold_no_concern() -> None:
    """2 business days is exactly the ``> 2`` boundary — no concern
    (strict `>`, exactly 2 doesn't fire)."""
    funder = _funder(funding_velocity_days=2)
    deal = _deal(urgency="ASAP (24-48 hours)")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("funding_velocity_mismatch" in c for c in match.soft_concerns)


def test_slow_funder_with_non_asap_urgency_no_concern() -> None:
    """``"This Week"`` urgency -> no rush -> no velocity concern even
    when the funder is slow."""
    funder = _funder(funding_velocity_days=5)
    deal = _deal(urgency="This Week")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("funding_velocity_mismatch" in c for c in match.soft_concerns)


def test_slow_funder_with_no_urgency_captured_no_concern() -> None:
    """Legacy merchant pre-urgency-capture: ``urgency=None`` -> the
    gate can't tell, so it skips (conservative — no false positive)."""
    funder = _funder(funding_velocity_days=5)
    deal = _deal(urgency=None)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("funding_velocity_mismatch" in c for c in match.soft_concerns)


def test_funder_with_no_velocity_published_skips_gate() -> None:
    """``funding_velocity_days=None`` -> the funder hasn't published a
    speed -> no soft concern even when merchant is ASAP."""
    funder = _funder(funding_velocity_days=None)
    deal = _deal(urgency="ASAP (24-48 hours)")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("funding_velocity_mismatch" in c for c in match.soft_concerns)


# ─────────────────────────────────────────────────────────────────────
# preferred_states — soft concern
# ─────────────────────────────────────────────────────────────────────


def test_state_in_preferred_list_no_concern() -> None:
    funder = _funder(preferred_states=("CA", "NV", "AZ"))
    deal = _deal(state="CA")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("state_not_preferred" in c for c in match.soft_concerns)


def test_state_not_in_preferred_list_soft_concern() -> None:
    """Funder prefers West-coast states; merchant is in TX (no NY
    broker-comp compliance gate to trip) -> soft concern. Deal still
    qualifies."""
    funder = _funder(preferred_states=("CA", "NV", "AZ"))
    deal = _deal(state="TX")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert match.match_score > 0
    assert any("state_not_preferred" in c and "TX" in c for c in match.soft_concerns)


def test_empty_preferred_states_skips_gate() -> None:
    """No preference published -> no concern."""
    funder = _funder(preferred_states=())
    deal = _deal(state="NY")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("state_not_preferred" in c for c in match.soft_concerns)


def test_preferred_match_is_case_insensitive() -> None:
    funder = _funder(preferred_states=("ca", "nv"))
    deal = _deal(state="CA")
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert not any("state_not_preferred" in c for c in match.soft_concerns)


def test_preferred_and_excluded_states_are_independent() -> None:
    """``preferred_states=("CA",)`` plus ``excluded_states=("TX",)`` —
    a CA merchant passes both; a TX merchant fails on excluded (hard)
    AND would fail on preferred (soft) — both surface. TX picked
    instead of NY to avoid the NY broker-comp compliance gate which
    would also fire for the NY case."""
    funder = _funder(
        preferred_states=("CA",),
        excluded_states=("TX",),
    )
    tx_deal = _deal(state="TX")
    match = match_funder(funder, tx_deal, _score())
    assert match is not None
    assert match.match_score == 0  # hard fail on excluded
    concerns = match.soft_concerns
    assert any("state_excluded" in c for c in concerns)
    assert any("state_not_preferred" in c for c in concerns)


# ─────────────────────────────────────────────────────────────────────
# Combined: all three new gates interacting
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("deal_type_in_list", "urgency_asap_slow", "state_preferred"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, True),
    ],
)
def test_three_new_gates_compose(
    deal_type_in_list: bool,
    urgency_asap_slow: bool,
    state_preferred: bool,
) -> None:
    funder = _funder(
        deal_types_accepted=("mca",) if deal_type_in_list else ("loc",),
        funding_velocity_days=5 if urgency_asap_slow else 1,
        preferred_states=("CA",) if state_preferred else (),
    )
    deal = _deal(
        deal_type="mca",
        urgency="ASAP (24-48 hours)" if urgency_asap_slow else None,
        state="CA",
    )
    match = match_funder(funder, deal, _score())
    assert match is not None

    has_deal_type_fail = any("deal_type_not_accepted" in c for c in match.soft_concerns)
    has_velocity_concern = any("funding_velocity_mismatch" in c for c in match.soft_concerns)
    has_state_concern = any("state_not_preferred" in c for c in match.soft_concerns)

    assert has_deal_type_fail is (not deal_type_in_list)
    assert has_velocity_concern is urgency_asap_slow
    assert has_state_concern is False  # state is always CA in this parametrize
