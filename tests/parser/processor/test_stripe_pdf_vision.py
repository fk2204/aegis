"""Stripe PDF extractor (vision-capable) tests.

The PDF path goes through Bedrock + the Stripe-specific extraction
prompt. We mock the LLM (no real Bedrock calls) and verify:
  * the Stripe extraction prompt — not the bank one or the Square one —
    is what's sent to the LLM
  * the mocked response hydrates cleanly into ``ExtractedProcessorStatement``
  * source attribution is preserved
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.parser.processor.extract_stripe import (
    STRIPE_EXTRACTION_PROMPT,
    ProcessorExtractionError,
    extract_stripe,
)


class _StripeLLMStub:
    """Captures the prompt; returns a canned Stripe extraction payload.

    Mirrors the LLMClient Protocol surface that the processor extractor
    uses (``extract_raw_json``). The captured prompt is what the test
    asserts against to confirm the Stripe-specific instructions land
    on the LLM call.
    """

    def __init__(self, payload: dict[str, Any], *, truncated: bool = False) -> None:
        self.payload = payload
        self.truncated = truncated
        self.last_prompt: str | None = None
        self.last_pdf_bytes_len: int | None = None

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        self.last_prompt = prompt
        self.last_pdf_bytes_len = len(pdf_bytes)
        return self.payload, self.truncated

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:  # pragma: no cover — unused on this path
        self.last_prompt = prompt
        return self.payload, self.truncated

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:  # pragma: no cover
        return {"classifications": []}


_CANNED_STRIPE_PAYLOAD: dict[str, Any] = {
    "summary": {
        "processor": "stripe",
        "business_name": "Acme Tech LLC",
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
        "gross_volume": "10000.00",
        "refunds_total": "200.00",
        "chargebacks_total": "100.00",
        "fees_total": "300.00",
        "payouts_total": "9400.00",
        "transaction_count": 4,
    },
    "transactions": [
        {
            "posted_date": "2026-03-05",
            "description": "Charge ch_test_001",
            "kind": "gross_charge",
            "amount": "10000.00",
            "source_page": 1,
            "source_line": 5,
        },
        {
            "posted_date": "2026-03-12",
            "description": "Refund re_test_001",
            "kind": "refund",
            "amount": "200.00",
            "source_page": 1,
            "source_line": 9,
        },
        {
            "posted_date": "2026-03-15",
            "description": "Dispute du_test_001",
            "kind": "chargeback",
            "amount": "100.00",
            "source_page": 1,
            "source_line": 12,
        },
        {
            "posted_date": "2026-03-15",
            "description": "Stripe processing fee",
            "kind": "fee",
            "amount": "300.00",
            "source_page": 1,
            "source_line": 15,
        },
        {
            "posted_date": "2026-03-31",
            "description": "Payout to bank",
            "kind": "payout",
            "amount": "9400.00",
            "source_page": 2,
            "source_line": 3,
        },
    ],
}


def test_extract_stripe_pdf_sends_stripe_specific_prompt() -> None:
    """Confirm the prompt sent to the LLM is the Stripe one. A test
    that just verified the result shape would pass even if the bank
    prompt got sent by mistake; capturing the prompt makes that bug
    impossible to hide."""
    llm = _StripeLLMStub(_CANNED_STRIPE_PAYLOAD)
    extract_stripe(b"%PDF-1.4 minimal stub bytes", llm)
    assert llm.last_prompt == STRIPE_EXTRACTION_PROMPT
    # Sanity-check the prompt's load-bearing instructions are present.
    assert '"processor": "stripe"' in llm.last_prompt
    assert "gross_charge" in llm.last_prompt
    assert "Stripe line items map to" in llm.last_prompt


def test_extract_stripe_pdf_hydrates_validated_statement() -> None:
    """The canned payload survives Pydantic validation and produces
    the expected ``ExtractedProcessorStatement`` shape."""
    llm = _StripeLLMStub(_CANNED_STRIPE_PAYLOAD)
    statement = extract_stripe(b"%PDF-1.4 minimal stub bytes", llm)
    assert statement.summary.processor == "stripe"
    assert statement.summary.business_name == "Acme Tech LLC"
    assert len(statement.transactions) == 5


def test_extract_stripe_pdf_preserves_source_attribution() -> None:
    """Every line item has source_page + source_line set per the LLM
    response — the validator gate enforces this downstream."""
    llm = _StripeLLMStub(_CANNED_STRIPE_PAYLOAD)
    statement = extract_stripe(b"%PDF-1.4 minimal stub bytes", llm)
    for row in statement.transactions:
        assert row.source_page >= 1
        assert row.source_line >= 1


def test_extract_stripe_pdf_rejects_empty_buffer() -> None:
    llm = _StripeLLMStub(_CANNED_STRIPE_PAYLOAD)
    with pytest.raises(ProcessorExtractionError, match="empty"):
        extract_stripe(b"", llm)


def test_extract_stripe_pdf_rejects_missing_keys() -> None:
    """LLM returned JSON that's missing ``summary`` or ``transactions`` —
    fail closed rather than silently producing an empty statement."""
    bad: dict[str, Any] = {"transactions": []}  # no summary
    llm = _StripeLLMStub(bad)
    with pytest.raises(ProcessorExtractionError, match="missing required keys"):
        extract_stripe(b"%PDF-1.4 stub", llm)
