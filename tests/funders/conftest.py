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
