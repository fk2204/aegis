"""Tests for the classification-confidence floor gate.

These verify the new accuracy gates in `parser/pipeline.py`:

- When the average classification confidence across all rows is below
  CLASSIFICATION_CONFIDENCE_FLOOR, the document hard-fails to
  manual_review with `classification_confidence_below_floor`.

- When any HIGH_IMPACT_CATEGORY (mca_debit, nsf_fee) has average
  confidence below its category floor, the document hard-fails even if
  the overall average is healthy. High-impact categories drive scoring
  hard-decline rules; low confidence there poisons the entire scoring
  chain.

The reconciliation gate and pattern detectors are exercised by the
existing e2e tests. These tests focus on the confidence path.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aegis.parser.pipeline import (
    CLASSIFICATION_CONFIDENCE_FLOOR,
    HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR,
    run_pipeline,
)
from tests.parser.conftest import _clean_extraction_payload, _StubLLM


class _LowConfidenceLLM(_StubLLM):
    """Stub LLM that classifies but returns a configurable confidence number."""

    def __init__(
        self,
        extraction_payload: dict[str, Any],
        *,
        confidence: int,
        per_category: dict[str, int] | None = None,
    ) -> None:
        super().__init__(extraction_payload)
        self._confidence = confidence
        self._per_category = per_category or {}

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        sentinel = "(JSON array follows):"
        idx = prompt.rfind(sentinel)
        if idx == -1:
            return {"classifications": []}
        tail = prompt[idx + len(sentinel) :].strip()
        rows = json.loads(tail)
        out: list[dict[str, Any]] = []
        for r in rows:
            desc = str(r.get("description", "")).lower()
            amt_str = str(r.get("amount", "0"))
            if amt_str.startswith("-"):
                category = "mca_debit" if "merchant advance" in desc else "fee"
            else:
                category = "deposit"
            confidence = self._per_category.get(category, self._confidence)
            out.append(
                {"id": r["id"], "category": category, "confidence": confidence}
            )
        return {"classifications": out}


def test_avg_confidence_below_floor_triggers_manual_review(
    clean_pdf_path: Path,
) -> None:
    """All rows at confidence 40 → avg 40 < floor 60 → manual_review."""
    llm = _LowConfidenceLLM(_clean_extraction_payload(), confidence=40)
    result = run_pipeline(str(clean_pdf_path), llm, today=date(2026, 2, 15))

    assert result.parse_status == "manual_review", (
        f"low avg confidence must hard-fail; got {result.parse_status} "
        f"flags={result.all_flags!r}"
    )
    assert result.avg_classification_confidence == 40
    assert any(
        "classification_confidence_below_floor" in f for f in result.all_flags
    ), result.all_flags


def test_avg_confidence_at_floor_does_not_trigger(
    clean_pdf_path: Path,
) -> None:
    """avg == floor (60) is OK; only avg < floor trips.

    Note: mca_debit is a HIGH_IMPACT_CATEGORY with its own floor (70), so
    we feed it ≥70 here and let the deposit rows sit at the avg floor to
    exercise the overall-avg gate at its edge.
    """
    llm = _LowConfidenceLLM(
        _clean_extraction_payload(),
        confidence=CLASSIFICATION_CONFIDENCE_FLOOR,
        per_category={"mca_debit": HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR},
    )
    result = run_pipeline(str(clean_pdf_path), llm, today=date(2026, 2, 15))
    assert all(
        "classification_confidence_below_floor" not in f for f in result.all_flags
    ), f"floor edge should not trip; flags={result.all_flags!r}"


def test_high_impact_category_below_floor_triggers_even_with_healthy_avg(
    clean_pdf_path: Path,
) -> None:
    """Deposits at 95, MCA at 50 → overall avg high but MCA below 70 → fail."""
    llm = _LowConfidenceLLM(
        _clean_extraction_payload(),
        confidence=95,  # default for non-mapped categories
        per_category={"mca_debit": 50},
    )
    result = run_pipeline(str(clean_pdf_path), llm, today=date(2026, 2, 15))
    assert result.parse_status == "manual_review", (
        f"low mca_debit confidence must hard-fail even with healthy avg; "
        f"got {result.parse_status} flags={result.all_flags!r}"
    )
    assert result.classification_confidence_by_category.get("mca_debit") == 50
    assert any(
        "classification_confidence_below_floor_mca_debit" in f
        for f in result.all_flags
    ), result.all_flags


def test_high_impact_category_at_floor_does_not_trigger(
    clean_pdf_path: Path,
) -> None:
    """mca_debit at exactly the per-category floor passes."""
    llm = _LowConfidenceLLM(
        _clean_extraction_payload(),
        confidence=95,
        per_category={"mca_debit": HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR},
    )
    result = run_pipeline(str(clean_pdf_path), llm, today=date(2026, 2, 15))
    assert all(
        "classification_confidence_below_floor_mca_debit" not in f
        for f in result.all_flags
    ), result.all_flags


def test_high_confidence_proceeds_normally(
    clean_pdf_path: Path,
    clean_llm: object,
) -> None:
    """Sanity: the existing clean fixture (95 confidence) does NOT trip the gate."""
    result = run_pipeline(str(clean_pdf_path), clean_llm, today=date(2026, 2, 15))  # type: ignore[arg-type]
    assert result.avg_classification_confidence == 95
    assert all(
        "classification_confidence_below_floor" not in f for f in result.all_flags
    ), result.all_flags


@pytest.mark.parametrize("missing_category", ["mca_debit", "nsf_fee"])
def test_missing_high_impact_category_no_op(
    clean_pdf_path: Path, missing_category: str
) -> None:
    """A merchant with no mca_debit rows should not trip the mca_debit floor.

    Per-category gate only fires when the category has at least one row.
    """
    # The clean fixture has mca_debit rows; build one with NO mca rows.
    payload = _clean_extraction_payload()
    # Strip MCA debits — replace the negative rows with a generic "fee" so
    # classification produces zero mca_debit rows.
    for txn in payload["transactions"]:
        if txn["description"].lower().startswith("merchant advance"):
            txn["description"] = "BANK FEE — MONTHLY"

    llm = _LowConfidenceLLM(payload, confidence=95)
    result = run_pipeline(str(clean_pdf_path), llm, today=date(2026, 2, 15))
    cat_map = result.classification_confidence_by_category
    if missing_category in cat_map:
        # If somehow the category is present, it must be healthy.
        assert cat_map[missing_category] >= HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR
    assert all(
        f"classification_confidence_below_floor_{missing_category}" not in f
        for f in result.all_flags
    ), result.all_flags
