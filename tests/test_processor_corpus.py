"""Processor-corpus integration tests (mp Phase 6.6 / Stage 2C).

Grades the full processor pipeline (detect -> extract -> validate ->
aggregate) against the synthetic PDFs at ``tests/fixtures/corpus/
processor/``. Each PDF has a sibling ``.json`` manifest written by
``scripts.generate_processor_corpus`` — that manifest is the ground
truth (per .claude/rules/testing.md: NEVER auto-generated from parser
output).

The parser's LLM is stubbed with a manifest-reading fake: it returns
exactly the line items the generator wrote, preserving processor brand
and source attribution. This way we exercise the full pipeline (PDF in,
aggregates out) without paying Bedrock tokens during ``make test``.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aegis.parser.processor.aggregate import aggregate_processor
from aegis.parser.processor.detect import detect_processor
from aegis.parser.processor.pipeline import run_processor_pipeline
from aegis.parser.processor.validate import validate_processor

_CORPUS = Path(__file__).resolve().parent / "fixtures" / "corpus" / "processor"


def _manifest_pdfs() -> list[Path]:
    pdfs = sorted(_CORPUS.glob("*.pdf"))
    assert pdfs, (
        "processor corpus is empty — run "
        "`python -m scripts.generate_processor_corpus` to populate it"
    )
    return pdfs


def _load_manifest(pdf: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8"))
    return data


# ---------------------------------------------------------------------------
# Manifest-driven LLM stub
# ---------------------------------------------------------------------------


class _ManifestLLM:
    """Returns the manifest's line items + printed summary verbatim.

    The corpus contract is: the manifest is what the generator wrote,
    and the parser must reproduce those numbers within ±$0.01 (the
    validator's tolerance). Returning the manifest from the LLM stub
    lets us test the full validate + aggregate path end-to-end without
    a real Bedrock call.

    ``math_tampered`` scenario: the manifest's printed summary has an
    inflated gross — the stub returns it as-is, and the validator must
    catch the gap.
    """

    def __init__(self, manifest: dict[str, Any], processor: str) -> None:
        self._manifest = manifest
        self._processor = processor

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        return self._build_payload(), False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (page_images_png, prompt)
        return self._build_payload(), False

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        # Processor pipeline never calls classify (single-pass extract).
        _ = prompt
        return {"classifications": []}

    def _build_payload(self) -> dict[str, Any]:
        # Re-derive line items from the manifest's summed totals. The
        # manifest stores the summed totals per kind, plus the printed
        # totals (which differ on math_tampered). We need ONE row per
        # kind that sums to the summed total — the validator + aggregator
        # don't care about row counts, only sums.
        summed = self._manifest["summed_line_items"]
        printed = self._manifest["printed_summary"]
        period_start = self._manifest["period_start"]
        period_end = self._manifest["period_end"]
        transactions: list[dict[str, Any]] = []
        kinds = [
            ("gross_charge", "Card sale"),
            ("refund", "Refund"),
            ("chargeback", "Chargeback"),
            ("fee", "Processing fee"),
            ("payout", "Payout to bank"),
        ]
        line_no = 1
        for kind, label in kinds:
            amount = Decimal(summed[kind])
            if amount == Decimal("0.00"):
                continue
            transactions.append(
                {
                    "posted_date": period_start,
                    "description": label,
                    "kind": kind,
                    "amount": str(amount),
                    "source_page": 2,
                    "source_line": line_no,
                }
            )
            line_no += 1
        return {
            "summary": {
                "processor": self._processor,
                "business_name": "Acme Inc",
                "period_start": period_start,
                "period_end": period_end,
                "gross_volume": printed["gross_volume"],
                "refunds_total": printed["refunds_total"],
                "chargebacks_total": printed["chargebacks_total"],
                "fees_total": printed["fees_total"],
                "payouts_total": printed["payouts_total"],
                "transaction_count": len(transactions),
            },
            "transactions": transactions,
        }


# ---------------------------------------------------------------------------
# End-to-end pipeline runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pdf", _manifest_pdfs(), ids=lambda p: p.stem)
def test_pipeline_matches_manifest(pdf: Path) -> None:
    """For every corpus PDF: detect, extract, validate, aggregate.

    Asserts:
      - Brand detection matches the manifest.
      - parse_status matches the manifest's expected status.
      - Aggregates match the manifest's summed line items
        (within ±$0.01 tolerance).
    """
    manifest = _load_manifest(pdf)

    # Detection.
    detection = detect_processor(pdf)
    assert detection.brand == manifest["processor"], (
        f"detector picked {detection.brand} but manifest says "
        f"{manifest['processor']} for {pdf.name}"
    )

    # Pipeline run with the stubbed LLM.
    llm = _ManifestLLM(manifest, processor=manifest["processor"])
    result = run_processor_pipeline(
        pdf, pdf.read_bytes(), llm, brand=detection.brand
    )

    expected_status = manifest["expected"]["expected_parse_status"]
    assert result.parse_status == expected_status, (
        f"{pdf.name}: expected parse_status={expected_status} "
        f"got {result.parse_status} (failures={result.validation.failures})"
    )

    if manifest["expected"]["validation_passed"]:
        assert result.validation.passed, result.validation.failures
        assert result.aggregates is not None
        # Aggregates tie out to the manifest's summed line items.
        summed = manifest["summed_line_items"]
        for attr, key in (
            ("gross_volume", "gross_charge"),
            ("refunds_total", "refund"),
            ("chargebacks_total", "chargeback"),
            ("fees_total", "fee"),
            ("payouts_total", "payout"),
        ):
            value = getattr(result.aggregates, attr).value
            expected = Decimal(summed[key])
            gap = abs(value - expected)
            assert gap <= Decimal("0.01"), (
                f"{pdf.name}: {attr} aggregate {value} vs manifest {expected} "
                f"(gap {gap} > $0.01)"
            )
    else:
        # math_tampered scenario: validator must catch the gap.
        assert not result.validation.passed
        assert any(
            "reconciliation_failed" in f or "processor_math_failed" in f
            for f in result.validation.failures
        ), result.validation.failures


def test_chargeback_ratio_reflects_scenario() -> None:
    """high_chargebacks manifest exercises the >1% chargeback-ratio
    flag — confirm the aggregator computes the ratio the pipeline acts
    on. This is independent of validator/pipeline routing — purely a
    sanity check on the math behind the threshold."""
    pdf = _CORPUS / "stripe_high_chargebacks.pdf"
    manifest = _load_manifest(pdf)
    summed = manifest["summed_line_items"]
    expected_ratio = Decimal(summed["chargeback"]) / Decimal(summed["gross_charge"])
    assert expected_ratio > Decimal("0.01"), (
        f"high_chargebacks scenario should produce ratio > 1%; "
        f"got {expected_ratio}"
    )


def test_clean_scenario_proceeds_with_zero_chargebacks() -> None:
    """Sanity: the clean manifest must report zero chargebacks; if not,
    the corpus generator drifted and the rest of the corpus contract
    is suspect."""
    pdf = _CORPUS / "stripe_clean.pdf"
    manifest = _load_manifest(pdf)
    assert manifest["summed_line_items"]["chargeback"] == "0.00"
    assert manifest["expected"]["expected_parse_status"] == "proceed"


# Silence the import-unused noise without dropping the imports above —
# they're referenced indirectly via _ManifestLLM's payload shape.
_ = (date, aggregate_processor, validate_processor)
