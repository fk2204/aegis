"""OFAC hard-gate in match_funder (2026-07-01 FIX C1).

A merchant with ``ofac_is_clear=False`` (positive SDN match on the
compliance screen) must never surface any funder match — the block
is regulatory, not per-funder criteria. The dossier already suppresses
the match panel display; this gate ensures every downstream code path
(deals API, calibration pre-fetch, ad-hoc scripts) sees the same
"no match" answer instead of returning FunderMatch objects that could
leak into ground-truth data.

Contract:
  * ``ofac_is_clear=False`` → returns ``None`` regardless of funder
    criteria alignment.
  * ``ofac_is_clear=True``  → normal matching proceeds.
  * ``ofac_is_clear=None``  → pass (never-screened merchants must not
    be silently dropped; screening happens on the next dossier open).
  * ``merchant=None``       → pass (legacy callers without merchant
    context, e.g. tests that only exercise the deal/score/funder
    triple).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult


def _baseline_deal() -> ScoreInput:
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="811111",
        industry_choice="Auto Repair / Service",
        time_in_business_months=36,
        credit_score=680,
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


def _qualifying_funder() -> FunderRow:
    return FunderRow(
        id=uuid4(),
        name="Test Funder",
        active=True,
        min_monthly_revenue=Decimal("50000.00"),
        min_credit_score=600,
        min_months_in_business=12,
        min_avg_daily_balance=Decimal("5000.00"),
        max_nsf_tolerance=3,
        max_positions=2,
        min_advance=Decimal("10000.00"),
        max_advance=Decimal("250000.00"),
    )


def _baseline_score() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def _make_merchant(*, ofac_is_clear: bool | None) -> MerchantRow:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Co",
        owner_name="Jane Doe",
        state="CA",
        industry_naics="811111",
        industry_choice="Auto Repair / Service",
        status="finalized",
        ofac_is_clear=ofac_is_clear,
        ofac_checked_at=now if ofac_is_clear is not None else None,
        created_at=now,
        updated_at=now,
    )


def test_ofac_match_blocks_qualifying_funder() -> None:
    """ofac_is_clear=False must return None even when the funder's
    criteria would otherwise qualify. This is the FIX C1 contract."""
    match = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
        merchant=_make_merchant(ofac_is_clear=False),
    )
    assert match is None, "OFAC match must block funder matching"


def test_ofac_clear_allows_normal_matching() -> None:
    """ofac_is_clear=True proceeds through normal matching."""
    match = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
        merchant=_make_merchant(ofac_is_clear=True),
    )
    assert match is not None
    assert match.funder_id is not None


def test_ofac_never_screened_passes_through() -> None:
    """ofac_is_clear=None (never screened) must not silently drop the
    merchant — screening happens on first dossier open. Gating on
    None would regress the screen-on-first-open ergonomics."""
    match = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
        merchant=_make_merchant(ofac_is_clear=None),
    )
    assert match is not None, "never-screened merchants must not be blocked by the OFAC gate"


def test_legacy_caller_without_merchant_passes_through() -> None:
    """Callers that pass merchant=None (unit tests exercising just the
    deal/score/funder triple) must not regress — the OFAC gate is a
    no-op without a merchant to check."""
    match = match_funder(
        _qualifying_funder(),
        _baseline_deal(),
        _baseline_score(),
    )
    assert match is not None
