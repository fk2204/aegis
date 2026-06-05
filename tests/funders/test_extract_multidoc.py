"""Tests for multi-doc funder extraction: image entry point + merge helper.

The Shor Capital accuracy retest surfaced a real workflow gap: funders
often distribute a guideline image (PNG/JPEG screenshot from Gmail) AND
a signed ISO PDF together. Processing only one loses half the picture
(revenue + industries on the PNG, factor band + contact on the ISO).
The merge helper here field-merges per-doc extractions so the operator
sees a single review form.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from aegis.funders.extract import (
    _MAX_IMAGE_BYTES,
    FunderExtractionError,
    extract_funder_guidelines,
    extract_funder_guidelines_from_image,
    merge_extractions,
)
from aegis.funders.models import FunderGuidelineExtraction


class _ImageStub:
    """Stub LLM whose vision path returns a canned payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        raise NotImplementedError

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (page_images_png, prompt)
        return self._payload, False

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError


_PNG_PAYLOAD: dict[str, Any] = {
    "draft": {
        "name": "Shor Capital",
        "min_monthly_revenue": 30000,
        "excluded_industries": [
            "trucking",
            "bail-bonds",
            "check-cashing",
        ],
        "auto_decline_conditions": ["Active bankruptcy"],
        "conditional_requirements": [],
        "tiers": [],
        "contact_name": "",
        "contact_phone": "",
        "contact_email": "",
        "submission_email": "",
        "notes_residual": "Same-day funding available.",
    },
    "confidence_by_field": {
        "min_monthly_revenue": 90,
        "excluded_industries": 88,
        "auto_decline_conditions": 70,
        "contact_email": 0,
    },
    "unparseable_fragments": [],
    "overall_confidence": 70,
}

_PDF_PAYLOAD: dict[str, Any] = {
    "draft": {
        "name": "Shor Capital",
        "min_monthly_revenue": 20000,
        "excluded_industries": ["TRUCKING", "law-firms"],
        "auto_decline_conditions": [],
        "conditional_requirements": [
            "Driver's license",
            "Voided check",
            "4 months bank statements",
        ],
        "typical_factor_low": 1.30,
        "typical_factor_high": 1.45,
        "tiers": [],
        "contact_name": "Iliya Mem",
        "contact_phone": "555-987-6543",
        "contact_email": "iliya@shor.capital",
        "submission_email": "submissions@shor.capital",
        "notes_residual": "Commission clawback within 30 days of default.",
    },
    "confidence_by_field": {
        "min_monthly_revenue": 60,
        "excluded_industries": 50,
        "conditional_requirements": 95,
        "typical_factor_low": 92,
        "typical_factor_high": 92,
        "contact_name": 95,
        "contact_email": 95,
        "submission_email": 95,
    },
    "unparseable_fragments": ["§5(b) — exclusive marketing window"],
    "overall_confidence": 85,
}


# -- image entry point -----------------------------------------------------


def test_extract_from_image_populates_draft() -> None:
    stub = _ImageStub(_PNG_PAYLOAD)
    result = extract_funder_guidelines_from_image(b"\x89PNG\r\n\x1a\n\x00\x00", stub)
    assert isinstance(result, FunderGuidelineExtraction)
    assert result.draft.name == "Shor Capital"
    assert result.draft.min_monthly_revenue == Decimal("30000")
    assert "trucking" in result.draft.excluded_industries


def test_extract_from_image_rejects_empty_buffer() -> None:
    stub = _ImageStub(_PNG_PAYLOAD)
    with pytest.raises(FunderExtractionError, match="empty"):
        extract_funder_guidelines_from_image(b"", stub)


def test_extract_from_image_rejects_oversize() -> None:
    stub = _ImageStub(_PNG_PAYLOAD)
    big = b"\x00" * (_MAX_IMAGE_BYTES + 1)
    with pytest.raises(FunderExtractionError, match="too large"):
        extract_funder_guidelines_from_image(big, stub)


def test_extract_from_image_stamps_provenance() -> None:
    stub = _ImageStub(_PNG_PAYLOAD)
    result = extract_funder_guidelines_from_image(b"\x89PNG-bytes", stub)
    assert result.draft.guidelines_extracted_at is not None
    assert result.draft.guidelines_source_pdf_hash is not None
    assert len(result.draft.guidelines_source_pdf_hash) == 64


# -- merge_extractions -----------------------------------------------------


def _png_extraction() -> FunderGuidelineExtraction:
    stub = _ImageStub(_PNG_PAYLOAD)
    return extract_funder_guidelines_from_image(b"\x89PNG-bytes", stub)


def _pdf_extraction() -> FunderGuidelineExtraction:
    from tests.funders.conftest import _StubLLM

    stub = _StubLLM(_PDF_PAYLOAD)
    return extract_funder_guidelines(b"%PDF-1.4\n%payload\n%%EOF\n", stub)


def test_merge_highest_confidence_wins_for_scalars() -> None:
    png = _png_extraction()
    pdf = _pdf_extraction()

    merged = merge_extractions([png, pdf])

    # PNG has 90 confidence on min_monthly_revenue (30000),
    # PDF has 60 (20000). PNG wins.
    assert merged.draft.min_monthly_revenue == Decimal("30000")
    # Contact fields: PNG has 0/missing confidence, PDF has 95. PDF wins.
    assert merged.draft.contact_name == "Iliya Mem"
    assert merged.draft.contact_email == "iliya@shor.capital"
    assert merged.draft.submission_email == "submissions@shor.capital"
    # Pricing envelope only on PDF.
    assert merged.draft.typical_factor_low == Decimal("1.30")
    assert merged.draft.typical_factor_high == Decimal("1.45")


def test_merge_unions_tuple_fields_case_insensitive() -> None:
    png = _png_extraction()
    pdf = _pdf_extraction()

    merged = merge_extractions([png, pdf])

    inds = merged.draft.excluded_industries
    # "trucking" (PNG) and "TRUCKING" (PDF) collapse to one entry.
    lowered = [i.lower() for i in inds]
    assert lowered.count("trucking") == 1
    # Distinct industries from both docs survive.
    assert "trucking" in lowered
    assert "bail-bonds" in lowered
    assert "check-cashing" in lowered
    assert "law-firms" in lowered
    # auto_decline (PNG) + conditional_requirements (PDF) are independent
    # tuples — both populate without collision.
    assert "Active bankruptcy" in merged.draft.auto_decline_conditions
    assert "Driver's license" in merged.draft.conditional_requirements
    assert "4 months bank statements" in merged.draft.conditional_requirements


def test_merge_concatenates_notes_residual() -> None:
    png = _png_extraction()
    pdf = _pdf_extraction()
    merged = merge_extractions([png, pdf])
    assert "Same-day funding available." in merged.draft.notes_residual
    assert "Commission clawback within 30 days of default." in merged.draft.notes_residual


def test_merge_unions_unparseable_fragments() -> None:
    png = _png_extraction()
    pdf = _pdf_extraction()
    merged = merge_extractions([png, pdf])
    assert "§5(b) — exclusive marketing window" in merged.unparseable_fragments


def test_merge_confidence_is_per_field_max() -> None:
    png = _png_extraction()
    pdf = _pdf_extraction()
    merged = merge_extractions([png, pdf])
    # min_monthly_revenue: max(90, 60) = 90
    assert merged.confidence_by_field["min_monthly_revenue"] == 90
    # excluded_industries: max(88, 50) = 88
    assert merged.confidence_by_field["excluded_industries"] == 88
    # contact_email: max(0, 95) = 95
    assert merged.confidence_by_field["contact_email"] == 95
    # typical_factor_low: only PDF, max(absent, 92) = 92
    assert merged.confidence_by_field["typical_factor_low"] == 92


def test_merge_overall_confidence_is_max() -> None:
    png = _png_extraction()
    pdf = _pdf_extraction()
    merged = merge_extractions([png, pdf])
    assert merged.overall_confidence == 85  # max(70, 85)


def test_merge_single_part_returns_unchanged() -> None:
    pdf = _pdf_extraction()
    merged = merge_extractions([pdf])
    assert merged is pdf


def test_merge_empty_sequence_raises() -> None:
    with pytest.raises(FunderExtractionError, match="at least one"):
        merge_extractions([])


def test_merge_picks_non_placeholder_name() -> None:
    """A part whose name is "Unknown Funder" must yield to a real name."""
    from tests.funders.conftest import _StubLLM

    placeholder_payload: dict[str, Any] = {
        "draft": {
            "name": "Unknown Funder",
            "excluded_industries": ["cannabis"],
            "accepts_stacking": False,
            "tiers": [],
        },
        "confidence_by_field": {"excluded_industries": 60},
        "unparseable_fragments": [],
        "overall_confidence": 40,
    }
    placeholder = extract_funder_guidelines(
        b"%PDF-1.4\n%placeholder\n%%EOF\n", _StubLLM(placeholder_payload)
    )

    pdf = _pdf_extraction()
    merged = merge_extractions([placeholder, pdf])
    assert merged.draft.name == "Shor Capital"


def test_merge_unions_tiers_by_name() -> None:
    """Same tier name across docs → de-duped; different tier names accumulate."""
    from tests.funders.conftest import _StubLLM

    tier_a_payload: dict[str, Any] = {
        "draft": {
            "name": "Multi-Tier Funder",
            "tiers": [
                {"name": "Elite", "buy_rate_low": 1.25, "buy_rate_high": 1.30},
                {"name": "A", "buy_rate_low": 1.30, "buy_rate_high": 1.35},
            ],
            "accepts_stacking": False,
            "excluded_industries": [],
            "excluded_states": [],
        },
        "confidence_by_field": {"tiers": 90},
        "unparseable_fragments": [],
        "overall_confidence": 80,
    }
    tier_b_payload: dict[str, Any] = {
        "draft": {
            "name": "Multi-Tier Funder",
            "tiers": [
                {"name": "Elite", "buy_rate_low": 1.25, "buy_rate_high": 1.30},
                {"name": "B", "buy_rate_low": 1.40, "buy_rate_high": 1.45},
            ],
            "accepts_stacking": False,
            "excluded_industries": [],
            "excluded_states": [],
        },
        "confidence_by_field": {"tiers": 85},
        "unparseable_fragments": [],
        "overall_confidence": 75,
    }
    a = extract_funder_guidelines(
        b"%PDF-1.4\n%a\n%%EOF\n", _StubLLM(tier_a_payload)
    )
    b = extract_funder_guidelines(
        b"%PDF-1.4\n%b\n%%EOF\n", _StubLLM(tier_b_payload)
    )

    merged = merge_extractions([a, b])
    names = [t.name for t in merged.draft.tiers]
    assert names == ["Elite", "A", "B"]


def test_merge_does_not_overwrite_when_confidence_zero() -> None:
    """A part with confidence 0 for a field must not clobber a populated peer."""
    from tests.funders.conftest import _StubLLM

    populated: dict[str, Any] = {
        "draft": {
            "name": "Test Funder",
            "min_credit_score": 650,
            "accepts_stacking": False,
            "excluded_industries": [],
            "excluded_states": [],
            "tiers": [],
        },
        "confidence_by_field": {"min_credit_score": 85},
        "unparseable_fragments": [],
        "overall_confidence": 80,
    }
    silent: dict[str, Any] = {
        "draft": {
            "name": "Test Funder",
            "min_credit_score": None,
            "accepts_stacking": False,
            "excluded_industries": [],
            "excluded_states": [],
            "tiers": [],
        },
        "confidence_by_field": {"min_credit_score": 0},
        "unparseable_fragments": [],
        "overall_confidence": 60,
    }
    a = extract_funder_guidelines(b"%PDF-1.4\n%a\n%%EOF\n", _StubLLM(populated))
    b = extract_funder_guidelines(b"%PDF-1.4\n%b\n%%EOF\n", _StubLLM(silent))

    merged = merge_extractions([a, b])
    assert merged.draft.min_credit_score == 650
