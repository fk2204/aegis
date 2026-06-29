"""Tests for `aegis.funders.guidelines_extract` (build plan 8.1).

Covers the sanitiser shape: Decimal-safe money round-trip, low-
confidence drop, invalid stacking_policy drop, unknown-field drop,
list dedupe, malformed-int drop, FICO range guard via Pydantic.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.funders.guidelines_extract import (
    FunderGuidelinesExtraction,
    GuidelinesExtractionError,
    extract_guidelines_from_pdf,
)


class _StubLLM:
    """Returns one canned payload + truncation flag."""

    def __init__(self, payload: dict[str, Any], truncated: bool = False) -> None:
        self._payload = payload
        self._truncated = truncated

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        return self._payload, self._truncated

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        raise NotImplementedError

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError


def _full_payload() -> dict[str, Any]:
    return {
        "min_revenue": "25000.00",
        "min_fico": 600,
        "min_tib_months": 12,
        "max_positions": 2,
        "stacking_policy": "not_allowed",
        "excluded_industries": ["cannabis", "adult-entertainment"],
        "excluded_states": ["CA", "NY"],
        "max_advance_amount": "500000.00",
        "notes": "Renewals after 50% paid down.",
        "confidences": {
            "min_revenue": 0.95,
            "min_fico": 0.92,
            "min_tib_months": 0.88,
            "max_positions": 0.85,
            "stacking_policy": 0.90,
            "excluded_industries": 0.80,
            "excluded_states": 0.85,
            "max_advance_amount": 0.90,
        },
    }


def test_happy_path_extracts_all_fields() -> None:
    llm = _StubLLM(_full_payload())
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.min_revenue == "25000.00"
    assert result.min_fico == 600
    assert result.min_tib_months == 12
    assert result.max_positions == 2
    assert result.stacking_policy == "not_allowed"
    assert result.excluded_industries == ["cannabis", "adult-entertainment"]
    assert result.excluded_states == ["CA", "NY"]
    assert result.max_advance_amount == "500000.00"
    assert result.notes == "Renewals after 50% paid down."
    assert result.fields_populated_count == 9


def test_low_confidence_fields_dropped() -> None:
    payload = _full_payload()
    payload["confidences"]["min_fico"] = 0.30  # below floor 0.5
    payload["confidences"]["max_positions"] = 0.49
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.min_fico is None
    assert result.max_positions is None
    # Other fields land normally.
    assert result.min_revenue == "25000.00"


def test_money_garbage_dropped() -> None:
    payload = _full_payload()
    payload["min_revenue"] = "garbage"
    payload["max_advance_amount"] = "1.2.3"
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.min_revenue is None
    assert result.max_advance_amount is None


def test_money_currency_symbols_stripped() -> None:
    payload = _full_payload()
    payload["min_revenue"] = "$25,000.00"
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.min_revenue == "25000.00"


def test_invalid_stacking_policy_dropped() -> None:
    payload = _full_payload()
    payload["stacking_policy"] = "sometimes"
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.stacking_policy is None


def test_fico_out_of_range_raises_validation() -> None:
    payload = _full_payload()
    payload["min_fico"] = 999  # past Pydantic range guard
    llm = _StubLLM(payload)
    with pytest.raises(GuidelinesExtractionError):
        extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)


def test_unknown_field_dropped() -> None:
    payload = _full_payload()
    payload["surprise_field"] = "hello"
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    # The Pydantic FunderGuidelinesExtraction has extra="forbid", so an
    # unknown field would raise — proving the sanitiser dropped it.
    assert result.min_revenue == "25000.00"


def test_excluded_industries_deduped_case_insensitive() -> None:
    payload = _full_payload()
    payload["excluded_industries"] = ["Cannabis", "cannabis", " ADULT-Entertainment ", "trucking"]
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    # First-casing-wins dedupe (Cannabis kept), trimmed values.
    assert result.excluded_industries == ["Cannabis", "ADULT-Entertainment", "trucking"]


def test_truncated_response_raises() -> None:
    llm = _StubLLM(_full_payload(), truncated=True)
    with pytest.raises(GuidelinesExtractionError) as exc_info:
        extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert "truncated" in str(exc_info.value).lower()


def test_empty_pdf_raises() -> None:
    llm = _StubLLM(_full_payload())
    with pytest.raises(GuidelinesExtractionError):
        extract_guidelines_from_pdf(b"", llm)


def test_oversized_pdf_raises() -> None:
    llm = _StubLLM(_full_payload())
    with pytest.raises(GuidelinesExtractionError):
        extract_guidelines_from_pdf(b"\x00" * (25 * 1024 * 1024 + 1), llm)


def test_notes_kept_without_confidence() -> None:
    # `notes` is operator-visible commentary; carry it even if the LLM
    # forgets to emit a confidence score for it.
    payload = _full_payload()
    payload["confidences"].pop("notes", None)
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.notes == "Renewals after 50% paid down."


def test_missing_confidence_drops_structured_field() -> None:
    payload = _full_payload()
    payload["confidences"].pop("min_fico", None)
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.min_fico is None


def test_fields_populated_count_empty() -> None:
    payload: dict[str, Any] = {"notes": ""}
    llm = _StubLLM(payload)
    result = extract_guidelines_from_pdf(b"%PDF-1.4\n%%EOF\n", llm)
    assert result.fields_populated_count == 0
    assert isinstance(result, FunderGuidelinesExtraction)
