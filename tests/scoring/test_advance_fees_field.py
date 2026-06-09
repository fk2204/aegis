"""R3.4 — ``ScoreInput.advance_fees_charged`` wires the FL / GA shadow flag.

Background
----------
R3.4 (commit ``34a6aaa``) added ``_state_disclosure_flag(merchant_state,
deal_state, advance_fees_charged)`` returning shadow flags for TX (HB 700
first-priority lien) and FL / GA (broker-advance-fee prohibition). The
helper landed but ``ScoreInput`` had no ``advance_fees_charged`` field, so
the ``score_deal`` callsite always passed ``None`` — the FL / GA branch
never fired through the live scorer.

These tests verify the field-wiring now that ``ScoreInput`` carries the
flag and the callsite reads ``deal.advance_fees_charged``. The flag is
still shadow-only: no change to ``tier``, ``recommendation``,
``hard_decline_reasons``, or any other field.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal


@pytest.fixture
def fresh_ofac(tmp_path: Path) -> Iterator[OFACClient]:
    """OFAC client with an empty SDN list — never matches."""
    import json
    from datetime import UTC, datetime

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


def _deal(*, state: str, advance_fees_charged: bool | None) -> ScoreInput:
    """Clean, score-passing ScoreInput pinned to a target ``state`` +
    ``advance_fees_charged`` value. All other fields chosen to avoid every
    hard-decline gate so the test focuses on the shadow-flag wiring."""
    return ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Doe",
        state=state,
        industry_naics="238320",
        industry_risk_tier="moderate",
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
        payroll_detected=True,
        returned_ach_count=0,
        customer_concentration_pct=25,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        fraud_score=10,
        eof_markers=1,
        validation_passed=True,
        extraction_confidence=95,
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
        advance_fees_charged=advance_fees_charged,
    )


_ADVANCE_FEE_FLAG = "state_enforcement_concern:FL_GA_advance_fee_prohibition"


# -- field defaults ---------------------------------------------------------


def test_advance_fees_charged_defaults_to_none() -> None:
    """The new field is optional; ``None`` is the unknown default."""
    deal = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme",
        owner_name="Owner",
        state="CA",
        avg_daily_balance=Decimal("10000.00"),
        true_revenue=Decimal("50000.00"),
        monthly_revenue=Decimal("50000.00"),
        lowest_balance=Decimal("1000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        fraud_score=10,
        requested_amount=Decimal("25000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )
    assert deal.advance_fees_charged is None


# -- live scorer fires the flag (FL / GA) -----------------------------------


def test_fl_with_advance_fees_emits_shadow_flag(fresh_ofac: OFACClient) -> None:
    """``state=FL`` + ``advance_fees_charged=True`` → FL/GA flag in shadow."""
    deal = _deal(state="FL", advance_fees_charged=True)
    result = score_deal(deal, ofac=fresh_ofac)
    assert _ADVANCE_FEE_FLAG in result.shadow_flags


def test_ga_with_advance_fees_emits_shadow_flag(fresh_ofac: OFACClient) -> None:
    """``state=GA`` + ``advance_fees_charged=True`` → same flag."""
    deal = _deal(state="GA", advance_fees_charged=True)
    result = score_deal(deal, ofac=fresh_ofac)
    assert _ADVANCE_FEE_FLAG in result.shadow_flags


# -- live scorer does NOT fire (non FL/GA, False, None) ---------------------


def test_ca_with_advance_fees_no_flag(fresh_ofac: OFACClient) -> None:
    """``state=CA`` is outside the FL / GA scope — no flag even when True."""
    deal = _deal(state="CA", advance_fees_charged=True)
    result = score_deal(deal, ofac=fresh_ofac)
    assert _ADVANCE_FEE_FLAG not in result.shadow_flags


def test_fl_without_advance_fees_no_flag(fresh_ofac: OFACClient) -> None:
    """``state=FL`` + ``advance_fees_charged=False`` → no flag."""
    deal = _deal(state="FL", advance_fees_charged=False)
    result = score_deal(deal, ofac=fresh_ofac)
    assert _ADVANCE_FEE_FLAG not in result.shadow_flags


def test_fl_with_unknown_advance_fees_no_flag(fresh_ofac: OFACClient) -> None:
    """``state=FL`` + ``advance_fees_charged=None`` → no flag (unknown
    never fires; that's the conservative default for pre-R3.4 callers)."""
    deal = _deal(state="FL", advance_fees_charged=None)
    result = score_deal(deal, ofac=fresh_ofac)
    assert _ADVANCE_FEE_FLAG not in result.shadow_flags


# -- no regression on the non-state outputs ---------------------------------


def test_fl_with_advance_fees_does_not_change_decision(
    fresh_ofac: OFACClient,
) -> None:
    """Shadow flag must NOT promote to a hard decline, tier change, or
    recommendation flip — the whole point of shadow mode."""
    with_fees = score_deal(_deal(state="FL", advance_fees_charged=True), ofac=fresh_ofac)
    without_fees = score_deal(_deal(state="FL", advance_fees_charged=False), ofac=fresh_ofac)
    assert with_fees.recommendation == without_fees.recommendation
    assert with_fees.tier == without_fees.tier
    assert with_fees.hard_decline_reasons == without_fees.hard_decline_reasons
    assert with_fees.score == without_fees.score
