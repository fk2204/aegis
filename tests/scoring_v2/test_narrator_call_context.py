"""Verify the funder-narrative prompt threads call transcripts through
the ``Call notes (flag contradictions with application data): ...``
block and elides the block entirely when transcripts are absent.

Contract:

  * With ``call_transcripts`` set → prompt contains
    ``"Call notes (flag contradictions with application data): "``
    followed by the truncated transcripts (cap 800 chars).
  * Without ``call_transcripts`` (``None`` / empty / whitespace) →
    prompt contains NO ``"Call notes"`` label at all. An empty label
    invites the model to fabricate details.

Uses the injected ``_StubBedrock`` client from the deal_summary tests
so no network hits.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from aegis.merchants.models import MerchantRow
from aegis.scoring.models import ScoreResult
from aegis.scoring_v2.balance_health import BalanceHealthAggregation
from aegis.scoring_v2.deal_summary import (
    CloseContext,
    generate_funder_narrative,
)
from aegis.scoring_v2.mca_stack import MCAStackAggregation


class _StubBedrock:
    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.last_prompt: str | None = None

    def generate_text(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.text


def _merchant() -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Inc.",
        state="MA",
        time_in_business_months=36,
    )


def _score() -> ScoreResult:
    return ScoreResult(
        score=72,
        tier="B",
        recommendation="approve",
        soft_concerns=[],
        hard_decline_reasons=[],
        suggested_max_advance=Decimal("40000.00"),
    )


def _mca_stack() -> MCAStackAggregation:
    return MCAStackAggregation(
        active_mca_count=0,
        mca_monthly_load=Decimal("0.00"),
        estimated_combined_holdback_pct=None,
        largest_single_mca_monthly=Decimal("0.00"),
    )


def _balance_health() -> BalanceHealthAggregation:
    return BalanceHealthAggregation(
        avg_daily_balance=Decimal("9000.00"),
        avg_daily_balance_source_ids=(),
        adb_as_pct_of_monthly_deposits=Decimal("0.15"),
        adb_as_pct_of_monthly_deposits_source_ids=(),
        negative_days=0,
        negative_days_source_ids=(),
        negative_days_trailing_3m=0,
        negative_days_trailing_3m_source_ids=(),
        lowest_balance=Decimal("2500.00"),
        lowest_balance_date=None,
        lowest_balance_source_ids=(),
        shadow_triggers=(),
    )


# ---------------------------------------------------------------------------
# Present path
# ---------------------------------------------------------------------------


def test_narrator_prompt_contains_call_notes_block_when_transcripts_present() -> None:
    client = _StubBedrock()
    generate_funder_narrative(
        merchant=_merchant(),
        score_result=_score(),
        mca_stack=_mca_stack(),
        balance_health=_balance_health(),
        offer=None,
        close_context=CloseContext(
            call_transcripts=(
                "[Call 2026-06-15 - 442s]\n"
                "Merchant confirmed 2 active MCAs, seasonal trough Jul-Aug"
            ),
        ),
        client=client,
    )
    prompt = client.last_prompt or ""
    assert "Call notes (flag contradictions with application data):" in prompt
    assert "Merchant confirmed 2 active MCAs" in prompt


def test_narrator_prompt_truncates_call_notes_at_800_chars() -> None:
    client = _StubBedrock()
    long_body = "X" * 1500
    generate_funder_narrative(
        merchant=_merchant(),
        score_result=_score(),
        mca_stack=_mca_stack(),
        balance_health=_balance_health(),
        offer=None,
        close_context=CloseContext(call_transcripts=long_body),
        client=client,
    )
    prompt = client.last_prompt or ""
    # The block is present.
    assert "Call notes (flag contradictions with application data):" in prompt
    # The transcript content is capped to 800 X's — not 1500.
    assert "X" * 800 in prompt
    assert "X" * 801 not in prompt


# ---------------------------------------------------------------------------
# Absent path
# ---------------------------------------------------------------------------


def test_narrator_prompt_no_bare_label_when_call_transcripts_absent() -> None:
    """CloseContext with ``call_transcripts=None`` must NOT emit a bare
    ``Call notes:`` label — the block is elided entirely."""
    client = _StubBedrock()
    generate_funder_narrative(
        merchant=_merchant(),
        score_result=_score(),
        mca_stack=_mca_stack(),
        balance_health=_balance_health(),
        offer=None,
        close_context=CloseContext(call_transcripts=None),
        client=client,
    )
    prompt = client.last_prompt or ""
    assert "Call notes" not in prompt


def test_narrator_prompt_no_bare_label_when_call_transcripts_whitespace() -> None:
    """An empty / whitespace-only ``call_transcripts`` is treated the
    same as ``None`` — the whole block is elided so the model doesn't
    fabricate a summary from nothing."""
    client = _StubBedrock()
    generate_funder_narrative(
        merchant=_merchant(),
        score_result=_score(),
        mca_stack=_mca_stack(),
        balance_health=_balance_health(),
        offer=None,
        close_context=CloseContext(call_transcripts="   \n\n  "),
        client=client,
    )
    prompt = client.last_prompt or ""
    assert "Call notes" not in prompt
