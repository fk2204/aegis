"""U14 / R3.4 — per-funder advance-fee shadow signal in ``match_funder``.

Background
----------
R3.4 / commit ``7fb28ef`` added ``ScoreInput.advance_fees_charged: bool | None``
and wired the deal-level FL / GA shadow flag through
``score._state_disclosure_flag`` (explicit-override path). But
``build_score_input`` cannot know which funder a deal will be submitted
to, so the deal-level flag stays ``None`` for the live pipeline.

This module covers the gap by evaluating the prohibition per-FunderMatch:
when a funder admits ``charges_merchant_advance_fees=True`` and the
merchant lives in a state whose broker statute prohibits the practice
(FL, GA), ``match_funder`` appends
``state_enforcement_concern:FL_GA_advance_fee_prohibition_for_this_funder``
to the FunderMatch's ``soft_concerns``.

Shadow-only: no hard fail, no tier change, no recommendation flip. The
operator reviews the concern before submitting. Per CLAUDE.md
"Decision-boundary changes — shadow-first".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from aegis.funders.models import FunderRow
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import ScoreInput, ScoreResult
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PER_FUNDER_FLAG = (
    "state_enforcement_concern:"
    "FL_GA_advance_fee_prohibition_for_this_funder"
)
_DEAL_LEVEL_FLAG = "state_enforcement_concern:FL_GA_advance_fee_prohibition"


def _deal(*, state: str, **overrides: object) -> ScoreInput:
    """Baseline ScoreInput pinned to ``state``. Overrides one field per test."""
    base = ScoreInput(
        merchant_id=uuid4(),
        business_name="Acme Painting LLC",
        owner_name="Jane Doe",
        state=state,
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
    tier: Literal["A", "B", "C", "D", "F"] = "C",
) -> ScoreResult:
    return ScoreResult(
        score=60,
        tier=tier,
        recommendation="approve",
        suggested_max_advance=Decimal("50000.00"),
        recommended_factor_rate=Decimal("1.30"),
        recommended_holdback_pct=Decimal("0.12"),
        estimated_payback_days=120,
    )


def _funder(
    *,
    charges_merchant_advance_fees: bool,
    **overrides: object,
) -> FunderRow:
    """Baseline funder with one criterion set so ``criteria_count > 0``
    even when no other gate triggers. The advance-fee branch is the
    target of the test, so we want the funder to clear all hard checks
    unless the test explicitly forces a fail elsewhere."""
    base = FunderRow(
        id=uuid4(),
        name="AdvanceFees Test Funder",
        min_monthly_revenue=Decimal("25000.00"),
        charges_merchant_advance_fees=charges_merchant_advance_fees,
    )
    return base.model_copy(update=overrides)


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


# ---------------------------------------------------------------------------
# Per-funder shadow flag fires for FL / GA
# ---------------------------------------------------------------------------


def test_fl_merchant_with_advance_fee_funder_emits_per_funder_flag() -> None:
    """``state=FL`` + ``charges_merchant_advance_fees=True`` →
    per-funder ``soft_concerns`` carries the FL / GA prohibition flag."""
    deal = _deal(state="FL")
    funder = _funder(charges_merchant_advance_fees=True)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert _PER_FUNDER_FLAG in match.soft_concerns


def test_ga_merchant_with_advance_fee_funder_emits_per_funder_flag() -> None:
    """``state=GA`` + ``charges_merchant_advance_fees=True`` → same flag."""
    deal = _deal(state="GA")
    funder = _funder(charges_merchant_advance_fees=True)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert _PER_FUNDER_FLAG in match.soft_concerns


# ---------------------------------------------------------------------------
# Negative cases: out-of-scope state / no advance fees / lowercase normalization
# ---------------------------------------------------------------------------


def test_ca_merchant_with_advance_fee_funder_no_per_funder_flag() -> None:
    """CA is outside the FL / GA scope — no per-funder flag even when
    the funder admits to charging advance fees."""
    deal = _deal(state="CA")
    funder = _funder(charges_merchant_advance_fees=True)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert _PER_FUNDER_FLAG not in match.soft_concerns


def test_fl_merchant_with_clean_funder_no_per_funder_flag() -> None:
    """``state=FL`` + ``charges_merchant_advance_fees=False`` → no flag.
    The prohibition only fires when the funder actively charges fees."""
    deal = _deal(state="FL")
    funder = _funder(charges_merchant_advance_fees=False)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert _PER_FUNDER_FLAG not in match.soft_concerns


def test_lowercase_fl_state_still_fires() -> None:
    """``state="fl"`` (lowercase) still triggers — the matcher uppercases
    before checking against the prohibition set. Matches the existing
    ``excluded_states`` case-insensitive convention."""
    deal = _deal(state="fl")
    funder = _funder(charges_merchant_advance_fees=True)
    match = match_funder(funder, deal, _score())
    assert match is not None
    assert _PER_FUNDER_FLAG in match.soft_concerns


# ---------------------------------------------------------------------------
# Shadow-mode discipline: per-funder flag does NOT promote to a hard fail
# ---------------------------------------------------------------------------


def test_per_funder_flag_does_not_promote_to_hard_fail() -> None:
    """The branch appends to ``soft_concerns`` only — ``qualifies`` and
    ``match_score`` must mirror the clean-funder case so the operator
    does not see the FunderMatch silently dropped from the matched-funders
    grid because of a shadow signal."""
    deal = _deal(state="FL")
    with_fees = match_funder(
        _funder(charges_merchant_advance_fees=True), deal, _score()
    )
    without_fees = match_funder(
        _funder(charges_merchant_advance_fees=False), deal, _score()
    )
    assert with_fees is not None
    assert without_fees is not None
    # Same qualifying surface: the shadow flag is annotation only.
    assert with_fees.match_score >= without_fees.match_score - 10  # one extra soft → -10
    assert with_fees.reasons == without_fees.reasons


# ---------------------------------------------------------------------------
# Regression: deal-level ScoreInput.advance_fees_charged still flows through
# ---------------------------------------------------------------------------


def test_deal_level_advance_fees_flag_still_works(fresh_ofac: OFACClient) -> None:
    """The explicit deal-level path through
    ``score._state_disclosure_flag`` must continue to surface its own
    shadow flag regardless of whether per-funder matching ran. This is
    the regression guard for the U1 / 7fb28ef wiring."""
    deal = _deal(state="FL", advance_fees_charged=True)
    result = score_deal(deal, ofac=fresh_ofac)
    assert _DEAL_LEVEL_FLAG in result.shadow_flags


def test_deal_level_flag_independent_of_per_funder_branch(
    fresh_ofac: OFACClient,
) -> None:
    """When ``ScoreInput.advance_fees_charged is None`` (the live default,
    because build_score_input cannot know the funder at input-build time)
    the deal-level path stays silent — the per-funder evaluation in
    ``match_funder`` is the only place the prohibition surfaces."""
    deal = _deal(state="FL", advance_fees_charged=None)
    result = score_deal(deal, ofac=fresh_ofac)
    assert _DEAL_LEVEL_FLAG not in result.shadow_flags


# ---------------------------------------------------------------------------
# U14 APR completeness — ScoreResult.apr is quantized to 4 decimal places so
# it satisfies the DecisionPayload.apr_calculated max_digits=8, decimal_places=4
# Pydantic constraint when the route forwards it to the snapshot. Regression
# guard for U8 / commit 5f14bbe + this U14 audit pass.
# ---------------------------------------------------------------------------


def test_score_apr_is_quantized_to_four_decimal_places(
    fresh_ofac: OFACClient,
) -> None:
    """``_compute_deal_apr`` returns ``apr.quantize(Decimal('0.0001'))`` so
    the DecisionPayload constraint never rejects the snapshot write."""
    deal = _deal(state="CA")
    result = score_deal(deal, ofac=fresh_ofac)
    if result.apr is None:
        pytest.skip("apr-not-computed path covered separately")
    # Decimal exponents are negative for fractional places; quantize to
    # 0.0001 yields exponent == -4.
    assert result.apr.as_tuple().exponent == -4, (
        f"apr {result.apr} not quantized to 4 decimal places "
        f"(exponent={result.apr.as_tuple().exponent})"
    )


def test_decline_path_carries_apr_none() -> None:
    """Audit guard: the hard-decline ``ScoreResult`` constructor omits
    ``apr=`` so the field defaults to None. Tightens the U8 contract —
    if a future edit accidentally sets ``apr`` on the decline branch
    the test fails loudly."""
    deal = _deal(state="CA", mca_positions=8)  # stacking → hard decline
    result = score_deal(deal, ofac=None)
    assert result.recommendation == "decline"
    assert result.apr is None
