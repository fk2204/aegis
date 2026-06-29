"""Unit tests for ``aegis.funders.note_generator.generate_funder_note``.

Covers the pure-function contract:

  * Bedrock invoked once with the tool-use envelope ``emit_funder_note``,
    temperature 0, and the system prompt that bans em-dashes + marketing
    language.
  * The returned tool input is unwrapped and the ``note`` string flows
    back unchanged when within the 300-1800 char band.
  * Pydantic length validation rejects responses that fall outside the
    band, raising ``FunderNoteGenerationError`` so the route can render
    an empty-state hint without overwriting any persisted state.
  * Bedrock exceptions are caught at the client boundary and re-raised
    as ``FunderNoteGenerationError`` (same band) so callers only need
    to catch one type.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis.funders.models import FunderRow
from aegis.funders.note_generator import (
    FunderNoteGenerationError,
    FunderNoteResponse,
    generate_funder_note,
)
from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.offer import OfferRecommendation
from aegis.storage import AnalysisRow


def _merchant() -> MerchantRow:
    return MerchantRow(
        business_name="Acme Logistics LLC",
        entity_type="llc",
        owner_name="Jane Owner",
        state="CA",
        time_in_business_months=24,
        credit_score=680,
        requested_amount=Decimal("75000"),
    )


def _analysis() -> AnalysisRow:
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        statement_period_start=date(2026, 3, 1),
        statement_period_end=date(2026, 3, 31),
        statement_days=31,
        beginning_balance=Decimal("12000.00"),
        ending_balance=Decimal("15000.00"),
        avg_daily_balance=Decimal("18000.00"),
        true_revenue=Decimal("85000.00"),
        monthly_revenue=Decimal("85000.00"),
        lowest_balance=Decimal("3500.00"),
        num_nsf=2,
        days_negative=0,
        mca_positions=1,
        mca_daily_total=Decimal("300.00"),
        debt_to_revenue=Decimal("0.10"),
    )


def _offer() -> OfferRecommendation:
    return OfferRecommendation(
        product_type="revenue_based",
        recommended_amount=Decimal("50000.00"),
        max_amount=Decimal("75000.00"),
        holdback_pct=Decimal("0.15"),
        rationale="sized at 1.0x monthly revenue ($85,000)",
    )


def _funder() -> FunderRow:
    return FunderRow(name="Quick Capital Funding")


# 350-char sample note that satisfies the 300-1800 band.
_GOOD_NOTE = (
    "Acme Logistics LLC is a California LLC with 24 months in business and "
    "$85,000 monthly true revenue. Average daily balance of $18,000 supports "
    "the requested advance. One existing MCA position and two NSF events in "
    "the period are noted. Document integrity verified clean. Proposing "
    "$50,000 advance at 1.0x revenue multiple sizing."
)


class _StubLLM:
    """Capture the call args and return a canned response."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or {"note": _GOOD_NOTE}

    def invoke_tool_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], str]:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tool_name": tool_name,
                "tool_schema": tool_schema,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return self._response, "us.anthropic.test-model"


class _RaisingLLM:
    def invoke_tool_json(self, **_kwargs: Any) -> tuple[dict[str, Any], str]:
        raise RuntimeError("bedrock unavailable")


def test_generate_funder_note_returns_validated_text() -> None:
    """Happy path — Bedrock returns a 300-1800 char note; the function
    returns it verbatim and the call shape carries the spec contract."""
    stub = _StubLLM()
    text = generate_funder_note(
        merchant=_merchant(),
        analysis=_analysis(),
        offer=_offer(),
        funder=_funder(),
        llm_client=stub,
    )
    assert text == _GOOD_NOTE
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["tool_name"] == "emit_funder_note"
    assert call["temperature"] == 0.0
    # User prompt carries the spec's structured context lines.
    assert "Acme Logistics LLC" in call["user_prompt"]
    assert "TIB: 24 months" in call["user_prompt"]
    assert "True Monthly Revenue: $85,000" in call["user_prompt"]
    assert "Average Daily Balance: $18,000" in call["user_prompt"]
    assert "Funder: Quick Capital Funding" in call["user_prompt"]
    # System prompt enforces the no-em-dash, no-marketing constraints.
    assert "NO em-dashes" in call["system_prompt"]
    assert "marketing language" in call["system_prompt"]


def test_generate_funder_note_threads_track_a_verdict_word() -> None:
    """``track_a_verdict`` projects into the prompt's integrity line."""
    stub = _StubLLM()
    _ = generate_funder_note(
        merchant=_merchant(),
        analysis=_analysis(),
        offer=_offer(),
        funder=_funder(),
        llm_client=stub,
        track_a_verdict=None,
    )
    # None collapses to "unverified" — explicit and grep-able in audit.
    assert "Document Integrity: unverified" in stub.calls[0]["user_prompt"]


def test_generate_funder_note_raises_on_response_too_short() -> None:
    """A 50-char Bedrock response (below the 300 floor) fails the
    Pydantic validation and surfaces as FunderNoteGenerationError."""
    stub = _StubLLM(response={"note": "too short"})
    with pytest.raises(FunderNoteGenerationError) as excinfo:
        generate_funder_note(
            merchant=_merchant(),
            analysis=_analysis(),
            offer=_offer(),
            funder=_funder(),
            llm_client=stub,
        )
    assert "validation" in str(excinfo.value).lower() or "string" in str(excinfo.value).lower()


def test_generate_funder_note_raises_on_response_too_long() -> None:
    """A 2000-char Bedrock response (above the 1800 cap) fails Pydantic
    validation and raises FunderNoteGenerationError."""
    stub = _StubLLM(response={"note": "x" * 2000})
    with pytest.raises(FunderNoteGenerationError):
        generate_funder_note(
            merchant=_merchant(),
            analysis=_analysis(),
            offer=_offer(),
            funder=_funder(),
            llm_client=stub,
        )


def test_generate_funder_note_raises_on_bedrock_error() -> None:
    """RuntimeError from Bedrock is re-raised as FunderNoteGenerationError
    so callers only need one exception type."""
    with pytest.raises(FunderNoteGenerationError) as excinfo:
        generate_funder_note(
            merchant=_merchant(),
            analysis=_analysis(),
            offer=_offer(),
            funder=_funder(),
            llm_client=_RaisingLLM(),
        )
    assert "bedrock_call_failed" in str(excinfo.value)


def test_funder_note_response_pydantic_length_band() -> None:
    """The Pydantic shape enforces the 300-1800 character band — this is
    the contract the dossier route trusts so a stale Bedrock SDK can't
    smuggle a 5000-char wall of text past validation."""
    FunderNoteResponse(note="x" * 300)  # min OK
    FunderNoteResponse(note="x" * 1800)  # max OK
    with pytest.raises(ValidationError):
        FunderNoteResponse(note="x" * 299)
    with pytest.raises(ValidationError):
        FunderNoteResponse(note="x" * 1801)
