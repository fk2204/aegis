"""LLM-driven funder guideline extraction tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from aegis.funders.extract import (
    FunderExtractionError,
    extract_funder_guidelines,
)
from aegis.funders.models import FunderGuidelineExtraction


def test_extract_populates_funder_row(stub_llm: object) -> None:
    pdf_bytes = b"%PDF-1.4\n%any-bytes-suffice-since-llm-is-stubbed\n%%EOF\n"
    result = extract_funder_guidelines(pdf_bytes, stub_llm)  # type: ignore[arg-type]

    assert isinstance(result, FunderGuidelineExtraction)
    assert result.draft.name == "Acme Capital Funding"
    assert result.draft.min_monthly_revenue == Decimal("25000.00")
    assert result.draft.min_credit_score == 580
    assert result.draft.accepts_stacking is False
    assert "trucking" in result.draft.excluded_industries
    assert "CA" in result.draft.excluded_states
    assert result.overall_confidence == 88
    assert result.unparseable_fragments == [
        "Renewals: case-by-case after 50% paid down",
    ]


def test_extract_stamps_provenance(stub_llm: object) -> None:
    """`guidelines_extracted_at` and `guidelines_source_pdf_hash` are set
    automatically so re-extraction can be detected."""
    pdf_bytes = b"%PDF-1.4\n%payload-A\n%%EOF\n"
    a = extract_funder_guidelines(pdf_bytes, stub_llm)  # type: ignore[arg-type]
    assert a.draft.guidelines_extracted_at is not None
    assert a.draft.guidelines_source_pdf_hash is not None
    assert len(a.draft.guidelines_source_pdf_hash) == 64  # sha256 hex


def test_extract_per_field_confidence_present(stub_llm: object) -> None:
    pdf_bytes = b"%PDF-1.4\n%payload\n%%EOF\n"
    result = extract_funder_guidelines(pdf_bytes, stub_llm)  # type: ignore[arg-type]
    # Every confidence key is a 0..100 int.
    for key, value in result.confidence_by_field.items():
        assert isinstance(value, int)
        assert 0 <= value <= 100, f"{key}={value} out of range"


def test_extract_rejects_empty_pdf(stub_llm: object) -> None:
    with pytest.raises(FunderExtractionError, match="empty"):
        extract_funder_guidelines(b"", stub_llm)  # type: ignore[arg-type]


def test_extract_rejects_oversize_pdf(stub_llm: object) -> None:
    big = b"\x00" * (26 * 1024 * 1024)
    with pytest.raises(FunderExtractionError, match="too large"):
        extract_funder_guidelines(big, stub_llm)  # type: ignore[arg-type]


def test_extract_rejects_missing_draft_key() -> None:
    class _BadStub:
        def extract_raw_json(
            self, pdf_bytes: bytes, prompt: str
        ) -> tuple[dict[str, Any], bool]:
            return {"confidence_by_field": {}}, False

        def classify_batch_json(self, prompt: str) -> dict[str, Any]:
            raise NotImplementedError

    with pytest.raises(FunderExtractionError, match="draft"):
        extract_funder_guidelines(b"%PDF-1.4\n%%EOF\n", _BadStub())


def test_extract_handles_null_money_fields() -> None:
    """A funder sheet that doesn't list min_monthly_revenue should produce None."""

    class _StubMissing:
        def extract_raw_json(
            self, pdf_bytes: bytes, prompt: str
        ) -> tuple[dict[str, Any], bool]:
            return (
                {
                    "draft": {
                        "name": "Sparse Fund",
                        "min_monthly_revenue": None,
                        "accepts_stacking": False,
                        "excluded_industries": [],
                        "excluded_states": [],
                    },
                    "confidence_by_field": {"min_monthly_revenue": 0},
                    "unparseable_fragments": [],
                    "overall_confidence": 30,
                },
                False,
            )

        def classify_batch_json(self, prompt: str) -> dict[str, Any]:
            raise NotImplementedError

    result = extract_funder_guidelines(b"%PDF-1.4\n%%EOF\n", _StubMissing())
    assert result.draft.min_monthly_revenue is None
    assert result.confidence_by_field.get("min_monthly_revenue") == 0
