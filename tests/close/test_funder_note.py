"""Tests for ``aegis.close.funder_note.format_funder_note``.

The note is a plain-text payload posted to the Close Lead activity
feed when the underwriter clicks "Submit to Funder". The formatter is
a pure function — these tests pin:

* the full-data shape produces every section in the documented order;
* every optional field can be omitted independently and the section
  drops cleanly (no blank "— : —" placeholders);
* the output is always ``<= MAX_NOTE_LENGTH`` (1500) characters,
  exercised across a permutation of inputs and a pathological
  many-funders case.

Pure-function discipline: no Close client, no DB, no audit. Each test
builds Pydantic-strict fixtures and asserts on the returned string.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.close.funder_note import MAX_NOTE_LENGTH, RenewalContext, format_funder_note
from aegis.merchants.models import MerchantRow
from aegis.scoring.models import FunderMatch, ScoreResult, TierMatch
from aegis.scoring_v2.balance_health import BalanceHealthAggregation
from aegis.scoring_v2.industry import IndustryTier
from aegis.scoring_v2.mca_stack import MCAStackAggregation
from aegis.scoring_v2.offer import DEFAULT_HOLDBACK_PCT, OfferRecommendation

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _merchant(
    *,
    business_name: str = "Acme Diner LLC",
    state: str | None = "CA",
    industry_choice: str | None = "Restaurant / Food Service",
    close_lead_id: str | None = "lead_abc",
) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        status="finalized",
        business_name=business_name,
        owner_name="Sam Owner",
        state=state,
        industry_choice=industry_choice,
        close_lead_id=close_lead_id,
    )


def _score_result(
    *,
    score: int = 78,
    tier: str = "B",
    hard_decline_reasons: list[str] | None = None,
    soft_concerns: list[str] | None = None,
    recommended_factor_rate: Decimal = Decimal("1.300"),
) -> ScoreResult:
    return ScoreResult(
        score=score,
        tier=tier,
        recommendation="approve",
        hard_decline_reasons=hard_decline_reasons or [],
        soft_concerns=soft_concerns or [],
        suggested_max_advance=Decimal("75000.00"),
        recommended_factor_rate=recommended_factor_rate,
        recommended_holdback_pct=Decimal("0.1500"),
        estimated_payback_days=120,
    )


def _offer(
    *,
    recommended_amount: Decimal = Decimal("60000.00"),
    max_amount: Decimal = Decimal("90000.00"),
    holdback_pct: Decimal = DEFAULT_HOLDBACK_PCT,
) -> OfferRecommendation:
    return OfferRecommendation(
        recommended_amount=recommended_amount,
        max_amount=max_amount,
        holdback_pct=holdback_pct,
        rationale="sized at 1.0x monthly revenue",
    )


def _mca_stack(
    *,
    active_mca_count: int = 2,
    combined_holdback_pct: Decimal | None = Decimal("28.5"),
    mca_monthly_load: Decimal = Decimal("12000.00"),
) -> MCAStackAggregation:
    return MCAStackAggregation(
        active_mca_count=active_mca_count,
        active_mca_source_ids=(),
        mca_monthly_load=mca_monthly_load,
        mca_monthly_load_source_ids=(),
        estimated_combined_holdback_pct=combined_holdback_pct,
        largest_single_mca_monthly=Decimal("8000.00") if active_mca_count else Decimal("0.00"),
        largest_single_mca_lender="KAPITUS" if active_mca_count else None,
        largest_single_mca_source_ids=(),
        shadow_triggers=(),
    )


def _balance_health(
    *,
    avg_daily_balance: Decimal = Decimal("18450.00"),
) -> BalanceHealthAggregation:
    return BalanceHealthAggregation(
        avg_daily_balance=avg_daily_balance,
        avg_daily_balance_source_ids=(),
        adb_as_pct_of_monthly_deposits=Decimal("12.50"),
        adb_as_pct_of_monthly_deposits_source_ids=(),
        negative_days=2,
        negative_days_source_ids=(),
        negative_days_trailing_3m=1,
        negative_days_trailing_3m_source_ids=(),
        lowest_balance=Decimal("-150.00"),
        lowest_balance_date=None,
        lowest_balance_source_ids=(),
        shadow_triggers=(),
    )


def _tier_match(name: str, qualifies: bool) -> TierMatch:
    return TierMatch(tier_name=name, qualifies=qualifies)


def _funder_match(
    *,
    name: str,
    match_score: int,
    tier_matches: list[TierMatch] | None = None,
) -> FunderMatch:
    return FunderMatch(
        funder_id=UUID("00000000-0000-0000-0000-000000000001"),
        funder_name=name,
        match_score=match_score,
        reasons=[],
        soft_concerns=[],
        tier_matches=tier_matches or [],
    )


def _full_inputs() -> dict[str, object]:
    return {
        "merchant": _merchant(),
        "score_result": _score_result(),
        "offer": _offer(),
        "mca_stack": _mca_stack(),
        "balance_health": _balance_health(),
        "industry_tier": "high_volatility",
        "matched_funders": [
            _funder_match(
                name="Onyx Funding",
                match_score=92,
                tier_matches=[
                    _tier_match("Elite", qualifies=False),
                    _tier_match("Standard", qualifies=True),
                ],
            ),
            _funder_match(
                name="Logic Advance",
                match_score=85,
                tier_matches=[_tier_match("MCA", qualifies=True)],
            ),
            _funder_match(name="Mainline Capital", match_score=77),
        ],
        "months_of_statements": 4,
        "true_revenue_monthly": Decimal("48000.00"),
        "integrity_verdict": "clean",
        "num_nsf": 1,
        "days_negative": 2,
    }


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_full_data_renders_every_section_in_order() -> None:
    text = format_funder_note(**_full_inputs())  # type: ignore[arg-type]
    assert len(text) <= MAX_NOTE_LENGTH

    # Identity line is first.
    lines = text.split("\n")
    assert lines[0].startswith("Acme Diner LLC")
    assert "CA" in lines[0]
    assert "Restaurant / Food Service" in lines[0]

    # Period + revenue
    assert any("4 months" in line for line in lines)
    assert any("48,000" in line for line in lines)

    # Cashflow
    assert any("ADB $18,450" in line for line in lines)
    assert any("NSFs 1/mo" in line for line in lines)
    assert any("neg days 2/mo" in line for line in lines)

    # Stack
    assert any("2 MCAs" in line for line in lines)
    assert any("combined holdback ~28.5%" in line for line in lines)

    # Verdict
    assert any("AEGIS 78/100" in line for line in lines)
    assert any("tier B" in line for line in lines)
    assert any("high volatility" in line for line in lines)
    assert any("integrity clean" in line for line in lines)

    # Offer
    assert any("Suggested: $60,000 advance" in line for line in lines)
    assert any("1.300x" in line for line in lines)
    assert any("15% holdback" in line for line in lines)

    # Top funders
    assert any(line.startswith("Top funders:") for line in lines)
    assert any("Onyx Funding [92] Standard" in line for line in lines)
    assert any("Logic Advance [85] MCA" in line for line in lines)
    assert any("Mainline Capital [77] tier n/a" in line for line in lines)


def test_no_score_result_collapses_verdict_and_offer_lines() -> None:
    inputs = _full_inputs()
    inputs["score_result"] = None
    inputs["offer"] = None
    text = format_funder_note(**inputs)  # type: ignore[arg-type]

    assert "AEGIS" not in text
    assert "Suggested" not in text
    # Identity still renders.
    assert text.startswith("Acme Diner LLC")
    # Industry + integrity collapse into a standalone bit.
    assert "industry high volatility" in text
    assert "integrity clean" in text


def test_no_balance_health_drops_cashflow_line() -> None:
    inputs = _full_inputs()
    inputs["balance_health"] = None
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "ADB" not in text
    # Adjacent sections still present.
    assert "2 MCAs" in text
    assert "AEGIS 78/100" in text


def test_no_mca_stack_renders_existing_stack_none() -> None:
    inputs = _full_inputs()
    inputs["mca_stack"] = _mca_stack(active_mca_count=0, combined_holdback_pct=None)
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "Existing stack: none" in text
    assert "MCAs" not in text


def test_no_matched_funders_drops_top_funders_section() -> None:
    inputs = _full_inputs()
    inputs["matched_funders"] = []
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "Top funders:" not in text


def test_no_state_or_industry_still_renders_identity() -> None:
    inputs = _full_inputs()
    inputs["merchant"] = _merchant(state=None, industry_choice=None)
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    first_line = text.split("\n")[0]
    assert "Acme Diner LLC" in first_line
    assert "industry unspecified" in first_line


def test_no_industry_tier_no_integrity_verdict_defaults() -> None:
    inputs = _full_inputs()
    inputs["industry_tier"] = None
    inputs["integrity_verdict"] = None
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    # Verdict line still renders with score + tier + unverified integrity.
    assert "AEGIS 78/100" in text
    assert "integrity unverified" in text
    # Industry token drops out (don't claim a tier that wasn't computed).
    assert "industry " not in text or "industry unspecified" in text


def test_hard_decline_reasons_truncated_to_top_4() -> None:
    inputs = _full_inputs()
    inputs["score_result"] = _score_result(
        hard_decline_reasons=[
            "ofac_sanctions_match",
            "mca_positions_excess",
            "days_negative_excess",
            "debt_to_revenue_excess",
            "fraud_score_high",
            "should_not_appear",
        ],
    )
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "Declines:" in text
    assert "should_not_appear" not in text


def test_soft_concerns_truncated_to_top_3() -> None:
    inputs = _full_inputs()
    inputs["score_result"] = _score_result(
        soft_concerns=[
            "thin_adb",
            "revenue_decline",
            "high_concentration",
            "should_not_appear_either",
        ],
    )
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "Concerns:" in text
    assert "should_not_appear_either" not in text


def test_funder_with_no_qualifying_tier_marked_no_qualifying_tier() -> None:
    inputs = _full_inputs()
    inputs["matched_funders"] = [
        _funder_match(
            name="No-Qualify Capital",
            match_score=60,
            tier_matches=[
                _tier_match("Premium", qualifies=False),
                _tier_match("Standard", qualifies=False),
            ],
        ),
    ]
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "No-Qualify Capital [60] no qualifying tier" in text


def test_minimal_inputs_returns_just_identity_line() -> None:
    text = format_funder_note(
        merchant=_merchant(state=None, industry_choice=None),
        score_result=None,
        offer=None,
        mca_stack=None,
        balance_health=None,
        industry_tier=None,
        matched_funders=[],
    )
    assert text == "Acme Diner LLC · industry unspecified"
    assert len(text) <= MAX_NOTE_LENGTH


def test_under_max_length_with_realistic_full_input() -> None:
    text = format_funder_note(**_full_inputs())  # type: ignore[arg-type]
    assert len(text) <= MAX_NOTE_LENGTH


def test_under_max_length_with_many_funder_matches() -> None:
    """Twenty matched funders with long names — formatter must still
    cap the output at 1500 chars by trimming sections from the bottom."""
    funders = [
        _funder_match(
            name=f"Long Funder Name Number {i:02d} LLC of Greater Metropolitan",
            match_score=90 - i,
            tier_matches=[_tier_match(f"Tier-{i}", qualifies=True)],
        )
        for i in range(20)
    ]
    inputs = _full_inputs()
    inputs["matched_funders"] = funders
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert len(text) <= MAX_NOTE_LENGTH


def test_under_max_length_pathological_long_business_name() -> None:
    """A merchant business name larger than the whole budget gets
    truncated with an ellipsis and the result still respects the cap."""
    huge_name = "X" * (MAX_NOTE_LENGTH + 500)
    text = format_funder_note(
        merchant=_merchant(business_name=huge_name, state=None, industry_choice=None),
        score_result=None,
        offer=None,
        mca_stack=None,
        balance_health=None,
        industry_tier=None,
        matched_funders=[],
    )
    assert len(text) <= MAX_NOTE_LENGTH
    # Last character is an ellipsis indicating truncation occurred.
    assert text.endswith("…")


def test_zero_factor_rate_drops_factor_token() -> None:
    inputs = _full_inputs()
    inputs["score_result"] = _score_result(recommended_factor_rate=Decimal("0"))
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "@ 0" not in text
    assert "Suggested:" in text


def test_industry_tier_underscore_rendered_as_space() -> None:
    """The IndustryTier string ``hard_decline_class`` reads cleanly as
    ``hard decline class`` in the note rather than leaking the raw enum."""
    inputs = _full_inputs()
    tier: IndustryTier = "hard_decline_class"
    inputs["industry_tier"] = tier
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert "industry hard decline class" in text
    assert "hard_decline_class" not in text


# ---------------------------------------------------------------------------
# Sprint 7B — renewal_context branch
# ---------------------------------------------------------------------------


def test_renewal_context_prepends_header_block() -> None:
    """When ``renewal_context`` is supplied, the note starts with the
    RENEWAL header (two lines + a ``---`` separator + a blank line)
    followed by the standard body."""
    inputs = _full_inputs()
    inputs["renewal_context"] = RenewalContext(
        original_funding_date=date(2025, 12, 1),
        original_amount=Decimal("50000.00"),
        months_since_funding=6,
    )
    text = format_funder_note(**inputs)  # type: ignore[arg-type]

    expected_prefix = (
        "RENEWAL — Previously funded 2025-12-01 for $50,000\n"
        "6 months since original funding\n"
        "\n"
        "---\n"
        "\n"
    )
    assert text.startswith(expected_prefix)
    # Standard body follows untouched after the header.
    assert "Acme Diner LLC" in text


def test_renewal_context_none_is_byte_identical_to_legacy() -> None:
    """``renewal_context=None`` (default) must produce identical output
    to the pre-Sprint-7 call. Pin the default and explicit-None paths."""
    inputs = _full_inputs()
    default_text = format_funder_note(**inputs)  # type: ignore[arg-type]
    inputs["renewal_context"] = None
    explicit_none_text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert default_text == explicit_none_text
    assert "RENEWAL —" not in default_text


def test_renewal_context_respects_max_length_cap() -> None:
    """Header + body must still respect the ``MAX_NOTE_LENGTH`` cap.
    Twenty long-named funder matches plus a header is the stress case;
    the header survives and trailing body sections drop first."""
    inputs = _full_inputs()
    inputs["matched_funders"] = [
        _funder_match(
            name=f"Long Funder Name Number {i:02d} LLC of Greater Metropolitan",
            match_score=90 - i,
            tier_matches=[_tier_match(f"Tier-{i}", qualifies=True)],
        )
        for i in range(20)
    ]
    inputs["renewal_context"] = RenewalContext(
        original_funding_date=date(2025, 12, 1),
        original_amount=Decimal("50000.00"),
        months_since_funding=6,
    )
    text = format_funder_note(**inputs)  # type: ignore[arg-type]
    assert len(text) <= MAX_NOTE_LENGTH
    # Header MUST survive trimming.
    assert text.startswith("RENEWAL — Previously funded 2025-12-01 for $50,000")
