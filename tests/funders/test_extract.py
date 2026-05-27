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

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, Any], bool]:
            raise NotImplementedError

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

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, Any], bool]:
            raise NotImplementedError

        def classify_batch_json(self, prompt: str) -> dict[str, Any]:
            raise NotImplementedError

    result = extract_funder_guidelines(b"%PDF-1.4\n%%EOF\n", _StubMissing())
    assert result.draft.min_monthly_revenue is None
    assert result.confidence_by_field.get("min_monthly_revenue") == 0


# --- step C: tiered extraction --------------------------------------------


def test_extract_tiered_payload_populates_tiers(stub_llm_tiered: object) -> None:
    pdf_bytes = b"%PDF-1.4\n%logic-advance\n%%EOF\n"
    result = extract_funder_guidelines(pdf_bytes, stub_llm_tiered)  # type: ignore[arg-type]

    assert result.draft.name == "Logic Advance Group"
    assert len(result.draft.tiers) == 4
    tier_names = [t.name for t in result.draft.tiers]
    assert tier_names == ["Elite", "A", "B", "C"]

    elite = result.draft.tiers[0]
    assert elite.buy_rate_low == Decimal("1.25")
    assert elite.buy_rate_high == Decimal("1.3")
    assert elite.min_monthly_revenue == Decimal("100000")
    assert elite.max_advance == Decimal("1500000")
    assert elite.max_holdback == Decimal("0.15")
    assert elite.min_credit_score == 700
    assert elite.min_months_in_business == 60
    assert elite.max_positions == 1


def test_extract_contact_fields(stub_llm_tiered: object) -> None:
    result = extract_funder_guidelines(
        b"%PDF-1.4\n%payload\n%%EOF\n",
        stub_llm_tiered,  # type: ignore[arg-type]
    )
    assert result.draft.contact_name == "James Doe"
    assert result.draft.contact_phone == "555-123-4567"
    assert result.draft.contact_email == "james@logicadvance.com"
    assert result.draft.submission_email == "iso@logicadvance.com"


def test_extract_auto_decline_and_conditional_lists(
    stub_llm_tiered: object,
) -> None:
    result = extract_funder_guidelines(
        b"%PDF-1.4\n%payload\n%%EOF\n",
        stub_llm_tiered,  # type: ignore[arg-type]
    )
    assert result.draft.auto_decline_conditions == (
        "Active tax liens > $25K",
        "Open bankruptcy",
    )
    assert result.draft.conditional_requirements == (
        "Trucking: 2 yr MVR clean",
        "Construction: WC certificate",
    )


def test_extract_notes_residual_lands_separately_from_notes(
    stub_llm_tiered: object,
) -> None:
    result = extract_funder_guidelines(
        b"%PDF-1.4\n%payload\n%%EOF\n",
        stub_llm_tiered,  # type: ignore[arg-type]
    )
    assert "Renewals available" in result.draft.notes_residual
    # notes is for operator-authored content — extraction does not touch it.
    assert result.draft.notes == ""


def test_extract_confidence_keys_include_new_fields(
    stub_llm_tiered: object,
) -> None:
    result = extract_funder_guidelines(
        b"%PDF-1.4\n%payload\n%%EOF\n",
        stub_llm_tiered,  # type: ignore[arg-type]
    )
    for key in (
        "contact_name",
        "contact_phone",
        "contact_email",
        "submission_email",
        "tiers",
        "auto_decline_conditions",
        "conditional_requirements",
    ):
        assert key in result.confidence_by_field, (
            f"new step-C confidence key {key!r} missing"
        )


def test_extract_tier_with_inverted_buy_rate_raises() -> None:
    """LLM emits a tier with buy_rate_low > buy_rate_high.

    Pydantic should surface the model_validator failure as
    FunderExtractionError so the operator sees the draft did not parse.
    """

    class _StubInverted:
        def extract_raw_json(
            self, pdf_bytes: bytes, prompt: str
        ) -> tuple[dict[str, Any], bool]:
            return (
                {
                    "draft": {
                        "name": "Backwards Funder",
                        "tiers": [
                            {
                                "name": "Bad",
                                "buy_rate_low": 1.40,
                                "buy_rate_high": 1.30,
                            }
                        ],
                        "accepts_stacking": False,
                        "excluded_industries": [],
                        "excluded_states": [],
                    },
                    "confidence_by_field": {},
                    "unparseable_fragments": [],
                    "overall_confidence": 50,
                },
                False,
            )

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, Any], bool]:
            raise NotImplementedError

        def classify_batch_json(self, prompt: str) -> dict[str, Any]:
            raise NotImplementedError

    with pytest.raises(FunderExtractionError, match="buy_rate_low"):
        extract_funder_guidelines(b"%PDF-1.4\n%%EOF\n", _StubInverted())


def test_extract_empty_tiers_array_is_valid() -> None:
    """Funder with no explicit tier structure → tiers == ()."""

    class _StubNoTiers:
        def extract_raw_json(
            self, pdf_bytes: bytes, prompt: str
        ) -> tuple[dict[str, Any], bool]:
            return (
                {
                    "draft": {
                        "name": "Single Tier Funder",
                        "tiers": [],
                        "accepts_stacking": False,
                        "excluded_industries": [],
                        "excluded_states": [],
                    },
                    "confidence_by_field": {"tiers": 100},
                    "unparseable_fragments": [],
                    "overall_confidence": 80,
                },
                False,
            )

        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, Any], bool]:
            raise NotImplementedError

        def classify_batch_json(self, prompt: str) -> dict[str, Any]:
            raise NotImplementedError

    result = extract_funder_guidelines(b"%PDF-1.4\n%%EOF\n", _StubNoTiers())
    assert result.draft.tiers == ()
