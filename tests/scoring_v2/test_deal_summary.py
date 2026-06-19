"""Tests for ``aegis.scoring_v2.deal_summary.generate_deal_summary``.

Pure-function unit tests. No LLM call. Drives the verdict / headline /
body / flags contracts across the three buckets the card surfaces:
clean, review, decline.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring.models import ScoreResult
from aegis.scoring_v2.balance_health import BalanceHealthAggregation
from aegis.scoring_v2.deal_summary import (
    CloseContext,
    DealSummary,
    generate_deal_summary,
)
from aegis.scoring_v2.mca_stack import MCAStackAggregation


def _clean_balance_health() -> BalanceHealthAggregation:
    return BalanceHealthAggregation(
        avg_daily_balance=Decimal("12000.00"),
        avg_daily_balance_source_ids=(),
        adb_as_pct_of_monthly_deposits=Decimal("0.18"),
        adb_as_pct_of_monthly_deposits_source_ids=(),
        negative_days=0,
        negative_days_source_ids=(),
        negative_days_trailing_3m=0,
        negative_days_trailing_3m_source_ids=(),
        lowest_balance=Decimal("3000.00"),
        lowest_balance_date=None,
        lowest_balance_source_ids=(),
        shadow_triggers=(),
    )


def _stressed_balance_health() -> BalanceHealthAggregation:
    return BalanceHealthAggregation(
        avg_daily_balance=Decimal("1500.00"),
        avg_daily_balance_source_ids=(),
        adb_as_pct_of_monthly_deposits=Decimal("0.04"),
        adb_as_pct_of_monthly_deposits_source_ids=(),
        negative_days=6,
        negative_days_source_ids=(),
        negative_days_trailing_3m=6,
        negative_days_trailing_3m_source_ids=(),
        lowest_balance=Decimal("-500.00"),
        lowest_balance_date=None,
        lowest_balance_source_ids=(),
        shadow_triggers=("low_adb_shadow:4%",),
    )


def _empty_mca_stack() -> MCAStackAggregation:
    return MCAStackAggregation(
        active_mca_count=0,
        mca_monthly_load=Decimal("0.00"),
        estimated_combined_holdback_pct=None,
        largest_single_mca_monthly=Decimal("0.00"),
    )


def _heavy_mca_stack() -> MCAStackAggregation:
    return MCAStackAggregation(
        active_mca_count=3,
        mca_monthly_load=Decimal("33000.00"),
        estimated_combined_holdback_pct=Decimal("0.42"),
        largest_single_mca_monthly=Decimal("15000.00"),
        largest_single_mca_lender="FUNDER_A",
    )


def _make_merchant(**kwargs: object) -> MerchantRow:
    base: dict[str, object] = {
        "id": uuid4(),
        "business_name": "Acme Inc.",
        "state": "MA",
        "time_in_business_months": 48,
    }
    base.update(kwargs)
    return MerchantRow(**base)


def _score(
    *,
    score: int = 78,
    recommendation: str = "approve",
    soft_concerns: tuple[str, ...] = (),
    hard_decline_reasons: tuple[str, ...] = (),
    suggested_max_advance: Decimal = Decimal("50000.00"),
) -> ScoreResult:
    return ScoreResult(
        score=score,
        tier="B",
        recommendation=recommendation,
        soft_concerns=list(soft_concerns),
        hard_decline_reasons=list(hard_decline_reasons),
        suggested_max_advance=suggested_max_advance,
    )


# ---------------------------------------------------------------------------
# Verdict resolution
# ---------------------------------------------------------------------------


def test_clean_verdict_for_approve_with_no_soft_concerns() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert isinstance(summary, DealSummary)
    assert summary.verdict == "clean"
    assert summary.headline.startswith("Strong deal")


def test_review_verdict_for_approve_with_soft_concerns() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(soft_concerns=("missing_credit_score",)),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert summary.verdict == "review"
    assert summary.headline.startswith("Needs review")


def test_decline_verdict_for_decline_recommendation() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(
            recommendation="decline",
            hard_decline_reasons=("ofac_sanctions_match",),
        ),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert summary.verdict == "decline"
    assert summary.headline.startswith("Decline")


# ---------------------------------------------------------------------------
# Body content
# ---------------------------------------------------------------------------


def test_body_mentions_business_name_adb_and_advance() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(business_name="Tasty Diner LLC"),
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert "Tasty Diner LLC" in summary.body
    assert "$12,000" in summary.body  # adb
    assert "$50,000" in summary.body  # advance


def test_body_calls_out_existing_mca_stack_when_loaded() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(soft_concerns=("close to capacity",)),
        mca_stack=_heavy_mca_stack(),
        balance_health=_stressed_balance_health(),
        close_context=CloseContext(),
    )
    assert "3 existing MCA positions" in summary.body
    assert "42%" in summary.body


def test_body_quotes_call_transcripts_when_present() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(
            call_transcripts=(
                "Owner mentioned seasonal slowdown in January which "
                "explains the lower deposits that month. Otherwise stable."
            )
        ),
    )
    assert "Call summary:" in summary.body
    assert "seasonal slowdown" in summary.body


def test_body_falls_back_to_notes_when_no_call() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(
            notes_summary="Operator note: merchant expanding to a second location next quarter."
        ),
    )
    assert "Recent Close note:" in summary.body
    assert "second location" in summary.body


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def test_flags_include_hard_declines_and_web_presence_flags() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(
            web_presence_flags=["bbb_unresolved_complaints", "active_lawsuits"],
        ),
        score_result=_score(
            recommendation="decline",
            hard_decline_reasons=("ofac_sanctions_match",),
        ),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert "ofac_sanctions_match" in summary.flags
    assert "web presence: bbb_unresolved_complaints" in summary.flags
    assert "web presence: active_lawsuits" in summary.flags


def test_flags_drop_noise_soft_concerns() -> None:
    """Noise prefixes like ``soft_score_below_threshold`` should not bubble
    into the summary card; the longer concern list still shows on the
    funder-match panels."""
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(
            soft_concerns=(
                "soft_score_below_threshold: score=62",
                "apr_not_computable: ...",
                "missing stip: 4 bank statements required",
                "missing_credit_score",
            ),
        ),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert "missing_credit_score" in summary.flags
    assert not any(f.startswith("soft_score_below_threshold") for f in summary.flags)
    assert not any(f.startswith("apr_not_computable") for f in summary.flags)
    assert not any(f.startswith("missing stip:") for f in summary.flags)


def test_flags_cap_at_six() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(
            recommendation="decline",
            hard_decline_reasons=tuple(f"hard_{i}" for i in range(10)),
        ),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert len(summary.flags) == 6


def test_flags_are_deduplicated() -> None:
    summary = generate_deal_summary(
        merchant=_make_merchant(),
        score_result=_score(
            soft_concerns=("active_lawsuits", "active_lawsuits"),
        ),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        close_context=CloseContext(),
    )
    assert summary.flags.count("active_lawsuits") == 1


# ---------------------------------------------------------------------------
# generate_funder_narrative
# ---------------------------------------------------------------------------


from aegis.scoring_v2.deal_summary import generate_funder_narrative  # noqa: E402


class _StubBedrock:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.last_prompt: str | None = None

    def generate_text(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return self.text


class _RaisingBedrock:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def generate_text(self, prompt: str) -> str:
        raise self.exc


def test_funder_narrative_happy_path() -> None:
    client = _StubBedrock(
        "Acme Inc. is a 4-year-old MA restaurant with $12K ADB and no "
        "existing MCAs. Cashflow profile is clean and the integrity "
        "score is strong; we recommend a $50K advance."
    )
    text = generate_funder_narrative(
        merchant=_make_merchant(business_name="Acme Inc."),
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        offer=None,
        close_context=CloseContext(),
        client=client,
    )
    assert "Acme Inc." in text
    assert client.calls == 1


def test_funder_narrative_empty_business_name_short_circuits() -> None:
    """Cheap MerchantRow construction with empty business_name fails
    Pydantic validation; we bypass via model_copy on a valid row."""
    merchant = _make_merchant()
    blank = merchant.model_copy(update={"business_name": "   "})
    client = _StubBedrock("never called")
    text = generate_funder_narrative(
        merchant=blank,
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        offer=None,
        close_context=CloseContext(),
        client=client,
    )
    assert text == ""
    assert client.calls == 0


def test_funder_narrative_bedrock_failure_returns_empty() -> None:
    client = _RaisingBedrock(RuntimeError("bedrock_unavailable"))
    text = generate_funder_narrative(
        merchant=_make_merchant(),
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        offer=None,
        close_context=CloseContext(),
        client=client,
    )
    assert text == ""


def test_funder_narrative_prompt_carries_key_facts() -> None:
    client = _StubBedrock("ok")
    generate_funder_narrative(
        merchant=_make_merchant(
            business_name="Tasty Diner LLC",
            industry_choice="Restaurant / Food Service",
            time_in_business_months=48,
        ),
        score_result=_score(),
        mca_stack=_heavy_mca_stack(),
        balance_health=_clean_balance_health(),
        offer=None,
        close_context=CloseContext(
            notes_summary="merchant expanding to a second location",
            call_transcripts="seasonal slowdown explanation",
        ),
        client=client,
    )
    prompt = client.last_prompt or ""
    assert "Tasty Diner LLC" in prompt
    assert "Restaurant / Food Service" in prompt
    assert "4 years" in prompt or "48" in prompt
    assert "3" in prompt  # mca count
    assert "42%" in prompt  # holdback
    assert "expanding to a second location" in prompt
    assert "seasonal slowdown" in prompt


def test_funder_narrative_caps_response_length() -> None:
    text = "X" * 10000
    out = generate_funder_narrative(
        merchant=_make_merchant(),
        score_result=_score(),
        mca_stack=_empty_mca_stack(),
        balance_health=_clean_balance_health(),
        offer=None,
        close_context=CloseContext(),
        client=_StubBedrock(text),
    )
    assert len(out) == 1500
