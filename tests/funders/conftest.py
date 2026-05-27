"""Funder test fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pikepdf
import pytest


@pytest.fixture(scope="session")
def small_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("guidelines") / "guidelines.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(str(p))
    pdf.close()
    return p


class _StubLLM:
    """Returns a canned extraction payload for funder guideline tests."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        return self._payload, False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        raise NotImplementedError("funder extraction does not run a vision pass")

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError("funder extraction does not run a classification pass")


@pytest.fixture
def realistic_guideline_payload() -> dict[str, Any]:
    return {
        "draft": {
            "name": "Acme Capital Funding",
            "min_monthly_revenue": 25000,
            "min_avg_daily_balance": 3000,
            "min_credit_score": 580,
            "min_months_in_business": 12,
            "max_positions": 1,
            "accepts_stacking": False,
            "min_advance": 5000,
            "max_advance": 250000,
            "max_nsf_tolerance": 5,
            "typical_factor_low": 1.25,
            "typical_factor_high": 1.42,
            "typical_holdback_low": 0.10,
            "typical_holdback_high": 0.18,
            "excluded_industries": ["adult-entertainment", "trucking", "gas-stations"],
            "excluded_states": ["CA", "NY", "VT"],
            "notes": "Focus on retail, restaurant, professional services.",
        },
        "confidence_by_field": {
            "min_monthly_revenue": 95,
            "min_avg_daily_balance": 90,
            "min_credit_score": 92,
            "min_months_in_business": 88,
            "max_positions": 80,
            "accepts_stacking": 95,
            "min_advance": 90,
            "max_advance": 90,
            "max_nsf_tolerance": 70,
            "typical_factor_low": 75,
            "typical_factor_high": 75,
            "typical_holdback_low": 65,
            "typical_holdback_high": 65,
            "excluded_industries": 85,
            "excluded_states": 80,
        },
        "unparseable_fragments": [
            "Renewals: case-by-case after 50% paid down",
        ],
        "overall_confidence": 88,
    }


@pytest.fixture
def stub_llm(realistic_guideline_payload: dict[str, Any]) -> Iterator[_StubLLM]:
    yield _StubLLM(json.loads(json.dumps(realistic_guideline_payload)))


@pytest.fixture
def realistic_tiered_guideline_payload() -> dict[str, Any]:
    """Logic Advance-shaped payload: 4 tiers + full contact block.

    Used by step C extraction tests to verify the new fields populate
    end-to-end through the prompt → JSON → FunderGuidelineExtraction
    pipeline. Not a real LLM call — the stub returns this as-is.
    """
    return {
        "draft": {
            "name": "Logic Advance Group",
            "contact_name": "James Doe",
            "contact_phone": "555-123-4567",
            "contact_email": "james@logicadvance.com",
            "submission_email": "iso@logicadvance.com",
            "min_monthly_revenue": 50000,
            "min_avg_daily_balance": 5000,
            "min_credit_score": 600,
            "min_months_in_business": 12,
            "max_positions": 3,
            "accepts_stacking": True,
            "min_advance": 10000,
            "max_advance": 1500000,
            "max_nsf_tolerance": 5,
            "typical_factor_low": 1.25,
            "typical_factor_high": 1.45,
            "typical_holdback_low": 0.10,
            "typical_holdback_high": 0.20,
            "excluded_industries": ["cannabis", "adult-entertainment"],
            "excluded_states": ["NV", "SD"],
            "tiers": [
                {
                    "name": "Elite",
                    "buy_rate_low": 1.25,
                    "buy_rate_high": 1.30,
                    "min_months_in_business": 60,
                    "min_credit_score": 700,
                    "min_monthly_revenue": 100000,
                    "max_positions": 1,
                    "max_advance": 1500000,
                    "max_holdback": 0.15,
                },
                {
                    "name": "A",
                    "buy_rate_low": 1.28,
                    "buy_rate_high": 1.35,
                    "min_months_in_business": 36,
                    "min_credit_score": 660,
                    "min_monthly_revenue": 75000,
                    "max_positions": 1,
                    "max_advance": 750000,
                    "max_holdback": 0.18,
                },
                {
                    "name": "B",
                    "buy_rate_low": 1.32,
                    "buy_rate_high": 1.42,
                    "min_months_in_business": 18,
                    "min_credit_score": 620,
                    "min_monthly_revenue": 50000,
                    "max_positions": 2,
                    "max_advance": 350000,
                    "max_holdback": 0.20,
                },
                {
                    "name": "C",
                    "buy_rate_low": 1.38,
                    "buy_rate_high": 1.45,
                    "min_months_in_business": 12,
                    "min_credit_score": 600,
                    "min_monthly_revenue": 30000,
                    "max_positions": 3,
                    "max_advance": 150000,
                    "max_holdback": 0.20,
                },
            ],
            "auto_decline_conditions": [
                "Active tax liens > $25K",
                "Open bankruptcy",
            ],
            "conditional_requirements": [
                "Trucking: 2 yr MVR clean",
                "Construction: WC certificate",
            ],
            "notes_residual": (
                "Renewals available after 50% paid down. Same-day funding "
                "for Elite tier when complete file is in by 11 AM ET."
            ),
        },
        "confidence_by_field": {
            "min_monthly_revenue": 95,
            "min_avg_daily_balance": 80,
            "min_credit_score": 92,
            "min_months_in_business": 90,
            "max_positions": 88,
            "accepts_stacking": 95,
            "min_advance": 92,
            "max_advance": 92,
            "max_nsf_tolerance": 70,
            "typical_factor_low": 85,
            "typical_factor_high": 85,
            "typical_holdback_low": 80,
            "typical_holdback_high": 80,
            "excluded_industries": 90,
            "excluded_states": 85,
            "contact_name": 95,
            "contact_phone": 95,
            "contact_email": 95,
            "submission_email": 95,
            "tiers": 92,
            "auto_decline_conditions": 90,
            "conditional_requirements": 88,
        },
        "unparseable_fragments": [],
        "overall_confidence": 92,
    }


@pytest.fixture
def stub_llm_tiered(
    realistic_tiered_guideline_payload: dict[str, Any],
) -> Iterator[_StubLLM]:
    yield _StubLLM(json.loads(json.dumps(realistic_tiered_guideline_payload)))
