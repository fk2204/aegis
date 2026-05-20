"""Two-pass LLM funder-reply extractor tests (mp Phase 10 / 2D-main).

Canned-LLM stub pattern — same shape as ``tests/funders/conftest.py``'s
``_StubLLM`` but tailored to the text-in/JSON-out surface
(``classify_batch_json``) the reply extractor uses.

These tests cover:

  * Pass 1 succeeds → result reflects the LLM's draft.
  * Pass 1 produces JSON that fails strict Pydantic validation (e.g.
    factor returned as a float) → pass 2 re-prompts → corrected result.
  * Pass 1 produces malformed JSON (raises ValueError) → pass 2
    re-prompts → success.
  * Pass 2 also fails → FunderReplyExtractionError surfaces.
  * Out-of-range factor (e.g. 5.0) on the strict ReplyTerms model is
    dropped per-field rather than voiding the whole extraction.

No real Bedrock calls. ``classify_batch_json`` is the only LLM method
the extractor invokes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from aegis.funders.reply_extract import (
    FunderReplyExtractionError,
    extract_funder_reply,
)


class _StubReplyLLM:
    """Sequence-of-responses stub for ``classify_batch_json``.

    Each call pops the next entry. An entry may be a dict (returned as
    JSON output) or a callable (invoked with the prompt — used to raise
    ValueError for the malformed-JSON path).
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.last_prompt: str | None = None

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        if not self._responses:
            raise AssertionError("stub LLM ran out of responses")
        self.call_count += 1
        self.last_prompt = prompt
        nxt = self._responses.pop(0)
        if callable(nxt):
            return nxt(prompt)  # type: ignore[no-any-return]
        return dict(nxt)

    # The Protocol requires these two; never called by the extractor.
    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        raise NotImplementedError

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        raise NotImplementedError


def _good_approved_payload() -> dict[str, Any]:
    return {
        "status": "approved",
        "decline_reason": None,
        "funder_name_text": "Acme Capital Funding",
        "terms": {
            "amount": "20000.00",
            "factor": "1.32",
            "payback": "26400.00",
            "term_days": 120,
            "daily_payment": "220.00",
            "holdback_pct": "0.12",
        },
        "parsed_confidence": 85,
        "notes": None,
    }


def test_extract_passes_through_on_clean_pass1() -> None:
    """Pass 1 returns a well-formed JSON object → no re-prompt."""
    stub = _StubReplyLLM([_good_approved_payload()])
    result = extract_funder_reply(
        "Funder approves at 1.32 factor, $20,000 advance, payback $26,400.",
        stub,
    )
    assert result.status == "approved"
    assert result.terms.amount == Decimal("20000.00")
    assert result.terms.factor == Decimal("1.32")
    assert result.terms.payback == Decimal("26400.00")
    assert result.terms.term_days == 120
    assert result.parsed_confidence == 85
    assert result.reprompted is False
    assert stub.call_count == 1


def test_extract_reprompt_on_invalid_pass1_output() -> None:
    """Pass-1 emits amount as a float (Pydantic strict rejects str
    field receiving float) → pass 2 re-prompts → success."""
    invalid_payload = _good_approved_payload()
    # Bare float — Pydantic strict will refuse this because amount is
    # str-typed in FunderReplyTermsDraft. The _coerce_terms helper
    # leaves floats untouched so the validator can reject them; this
    # is the trigger for the pass-2 re-prompt path.
    invalid_payload["terms"]["amount"] = 20000.0
    # Pass 2 returns the corrected output.
    stub = _StubReplyLLM([invalid_payload, _good_approved_payload()])

    result = extract_funder_reply("Approval email body", stub)
    assert result.status == "approved"
    assert result.terms.amount == Decimal("20000.00")
    assert result.reprompted is True
    assert stub.call_count == 2


def test_extract_reprompt_on_malformed_pass1_json() -> None:
    """If pass 1 raises ValueError (malformed JSON), the extractor
    re-prompts and the second attempt succeeds."""

    def raise_value_error(prompt: str) -> dict[str, Any]:
        raise ValueError("no JSON object in LLM response")

    stub = _StubReplyLLM([raise_value_error, _good_approved_payload()])
    result = extract_funder_reply("Approval email body", stub)
    assert result.reprompted is True
    assert result.terms.amount == Decimal("20000.00")
    assert stub.call_count == 2


def test_extract_raises_when_pass2_also_fails() -> None:
    """If pass-1 output is malformed AND pass 2 is also malformed,
    the extractor raises FunderReplyExtractionError instead of
    persisting garbage."""

    def raise_value_error(prompt: str) -> dict[str, Any]:
        raise ValueError("still no JSON object")

    stub = _StubReplyLLM([raise_value_error, raise_value_error])
    with pytest.raises(FunderReplyExtractionError, match="malformed JSON twice"):
        extract_funder_reply("Approval email body", stub)


def test_extract_raises_when_pass2_validation_fails() -> None:
    """Pass 1 invalid + pass 2 also invalid → extraction error."""
    invalid = _good_approved_payload()
    invalid["terms"]["amount"] = 20000.0  # float, will trigger reprompt
    still_invalid = _good_approved_payload()
    still_invalid["terms"]["factor"] = 99.0  # float again
    stub = _StubReplyLLM([invalid, still_invalid])
    with pytest.raises(FunderReplyExtractionError, match="pass-2 still failed"):
        extract_funder_reply("Approval email body", stub)


def test_extract_drops_out_of_range_factor_individually() -> None:
    """ReplyTerms enforces factor in [1.0, 2.0]. The LLM emitting a
    parseable-but-out-of-range factor should drop only that field, not
    the rest of the terms. Reconcile then surfaces missing factor as
    a warning at the deterministic gate (not at the LLM layer)."""
    payload = _good_approved_payload()
    payload["terms"]["factor"] = "5.0"  # out of [1, 2]
    stub = _StubReplyLLM([payload])
    result = extract_funder_reply("Approval email body", stub)
    assert result.terms.factor is None  # dropped
    assert result.terms.amount == Decimal("20000.00")  # preserved
    assert result.terms.payback == Decimal("26400.00")  # preserved


def test_extract_unknown_status_flows_through() -> None:
    """If the LLM says it can't tell whether the email is an offer or
    decline, the extractor returns status='unknown' — the worker
    audits and drops rather than persisting a wrong-status row."""
    payload = _good_approved_payload()
    payload["status"] = "unknown"
    payload["terms"] = {}
    payload["parsed_confidence"] = 10
    stub = _StubReplyLLM([payload])
    result = extract_funder_reply("Ambiguous body", stub)
    assert result.status == "unknown"
    assert result.parsed_confidence == 10


def test_extract_rejects_empty_text() -> None:
    with pytest.raises(FunderReplyExtractionError, match="empty"):
        extract_funder_reply("", _StubReplyLLM([]))


def test_extract_rejects_oversize_text() -> None:
    """A pathological 1MB paste should fail fast before the LLM call."""
    big = "a" * (65 * 1024)
    with pytest.raises(FunderReplyExtractionError, match="exceeds"):
        extract_funder_reply(big, _StubReplyLLM([]))


def test_extract_handles_declined_with_reason() -> None:
    """Declined replies don't carry offer terms but should still
    populate decline_reason."""
    payload = {
        "status": "declined",
        "decline_reason": "NSF count exceeds threshold",
        "funder_name_text": "Acme",
        "terms": {},
        "parsed_confidence": 90,
        "notes": None,
    }
    stub = _StubReplyLLM([payload])
    result = extract_funder_reply("We decline because of NSF count.", stub)
    assert result.status == "declined"
    assert result.decline_reason == "NSF count exceeds threshold"
    assert result.terms.amount is None
