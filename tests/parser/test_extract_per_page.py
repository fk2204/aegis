"""Per-page extraction merge tests (mp Phase 6.5).

``extract_statement_per_page`` slices the PDF by contiguous
same-strategy page groups, runs the appropriate extractor per group,
remaps ``source_page`` back to the original 1-indexed page number,
and merges the transaction lists. The validation gate then runs
against the merged statement.

This file does NOT call Bedrock — it injects a counted-call LLM stub
that returns different payloads on text vs vision calls so the merge
math is verifiable end-to-end.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pikepdf
import pymupdf
import pytest

from aegis.parser.extract import (
    ExtractionError,
    _group_pages_by_strategy,
    _remap_txn_source_pages,
    _slice_pdf,
    extract_statement_per_page,
)
from aegis.parser.models import ExtractedStatement, Transaction
from aegis.parser.page_router import PageStrategyDecision

# ---------------------------------------------------------------------------
# PDF builder — a real 4-page PDF (2 text, 2 image) for the slice tests
# ---------------------------------------------------------------------------


def _build_mixed_pdf(path: Path) -> None:
    """Pages: [text, text, image, image]. Real PDF so pikepdf slicing works.

    pymupdf has no type stubs — annotations stay implicit (mypy infers
    Any) per the existing test_text_layer_detection.py pattern.
    """
    out = pymupdf.open()
    for _ in range(2):
        p = out.new_page(width=612, height=792)
        p.insert_text(
            (72, 72),
            "\n".join(
                f"01/{i:02d}/2026 DEPOSIT 1000.00" for i in range(1, 21)
            ),
            fontsize=10,
        )
    # Image-only pages 3-4.
    text_src = pymupdf.open()
    src = text_src.new_page(width=612, height=792)
    src.insert_text((72, 72), "Image-page content\n" * 5, fontsize=11)
    pix = src.get_pixmap(dpi=150)
    text_src.close()
    for _ in range(2):
        ip = out.new_page(width=pix.width, height=pix.height)
        ip.insert_image(ip.rect, pixmap=pix)
    out.save(path)
    out.close()


# ---------------------------------------------------------------------------
# Counted-call LLM stub
# ---------------------------------------------------------------------------


def _summary(deposit_total: str, withdrawal_total: str) -> dict[str, Any]:
    return {
        "bank_name": "TEST BANK",
        "account_holder": "ACME CO LLC",
        "account_last4": "1234",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "beginning_balance": "5000.00",
        "ending_balance": "15000.00",
        "deposit_total": deposit_total,
        "withdrawal_total": withdrawal_total,
        "printed_transaction_count": 4,
    }


def _txn(
    posted_date: str, description: str, amount: str, source_page: int, source_line: int
) -> dict[str, Any]:
    return {
        "posted_date": posted_date,
        "description": description,
        "amount": amount,
        "source_page": source_page,
        "source_line": source_line,
        "running_balance": "9999.99",
    }


class _CountedLLM:
    """LLM stub that returns a different payload per call type.

    Pass ``text_payloads`` and ``vision_payloads`` as ordered queues;
    each call to extract_raw_json / extract_raw_json_from_images pops
    the next payload. Test cases must supply the right count.
    """

    def __init__(
        self,
        text_payloads: list[dict[str, Any]],
        vision_payloads: list[dict[str, Any]],
    ) -> None:
        self.text_payloads = list(text_payloads)
        self.vision_payloads = list(vision_payloads)
        self.text_calls = 0
        self.vision_calls = 0

    def extract_raw_json(
        self, pdf_bytes: bytes, prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        self.text_calls += 1
        return self.text_payloads.pop(0), False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (page_images_png, prompt)
        self.vision_calls += 1
        return self.vision_payloads.pop(0), False

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        """Unused in extract-only tests, but the LLMClient protocol
        requires it. Returns an empty classifications list so any
        downstream caller fails loudly rather than getting silent
        wrong data."""
        _ = prompt
        return {"classifications": []}


# ---------------------------------------------------------------------------
# Helpers (unit-level)
# ---------------------------------------------------------------------------


def test_group_pages_by_strategy_batches_consecutive_same() -> None:
    decisions = [
        PageStrategyDecision(0, "text", 500, 90, 40),
        PageStrategyDecision(1, "text", 600, 92, 40),
        PageStrategyDecision(2, "vision", 5, 10, 70),
        PageStrategyDecision(3, "vision", 5, 10, 70),
        PageStrategyDecision(4, "text", 700, 94, 40),
    ]
    groups = _group_pages_by_strategy(decisions)
    assert groups == [
        ("text", [0, 1]),
        ("vision", [2, 3]),
        ("text", [4]),
    ]


def test_remap_txn_source_pages_translates_local_to_original() -> None:
    """Slice pages [0, 3, 5] → 1-indexed slice pages 1, 2, 3 → original
    1-indexed 1, 4, 6."""
    txns = [
        Transaction(
            posted_date=date(2026, 1, 1),
            description="A",
            amount=Decimal("100.00"),
            source_page=1,  # first page of slice → original page 0 → 1-indexed 1
            source_line=1,
        ),
        Transaction(
            posted_date=date(2026, 1, 2),
            description="B",
            amount=Decimal("200.00"),
            source_page=2,  # second page of slice → original page 3 → 1-indexed 4
            source_line=1,
        ),
        Transaction(
            posted_date=date(2026, 1, 3),
            description="C",
            amount=Decimal("300.00"),
            source_page=3,  # third page of slice → original page 5 → 1-indexed 6
            source_line=1,
        ),
    ]
    remapped = _remap_txn_source_pages(txns, original_page_indices=[0, 3, 5])
    assert [t.source_page for t in remapped] == [1, 4, 6]


def test_remap_keeps_out_of_range_pages_for_validator_to_catch() -> None:
    """LLM hallucinated a source_page beyond the slice → preserved
    as-is so the source-attribution check fires downstream rather than
    silently dropping the row."""
    txns = [
        Transaction(
            posted_date=date(2026, 1, 1),
            description="X",
            amount=Decimal("50.00"),
            source_page=99,  # slice only has 2 pages → out of range
            source_line=1,
        ),
    ]
    remapped = _remap_txn_source_pages(txns, original_page_indices=[0, 1])
    assert remapped[0].source_page == 99


def test_slice_pdf_extracts_requested_pages(tmp_path: Path) -> None:
    src_path = tmp_path / "src.pdf"
    _build_mixed_pdf(src_path)
    pdf_bytes = src_path.read_bytes()
    sliced = _slice_pdf(pdf_bytes, page_indices=[0, 2])
    with pikepdf.open(io.BytesIO(sliced)) as out:
        assert len(out.pages) == 2


def test_slice_pdf_rejects_out_of_range_index(tmp_path: Path) -> None:
    src_path = tmp_path / "src.pdf"
    _build_mixed_pdf(src_path)
    pdf_bytes = src_path.read_bytes()
    with pytest.raises(ExtractionError):
        _slice_pdf(pdf_bytes, page_indices=[0, 99])


def test_slice_pdf_rejects_empty_index_list(tmp_path: Path) -> None:
    src_path = tmp_path / "src.pdf"
    _build_mixed_pdf(src_path)
    pdf_bytes = src_path.read_bytes()
    with pytest.raises(ExtractionError):
        _slice_pdf(pdf_bytes, page_indices=[])


# ---------------------------------------------------------------------------
# End-to-end merge
# ---------------------------------------------------------------------------


def test_per_page_merge_unions_transactions_and_remaps_pages(tmp_path: Path) -> None:
    """Pages [text, text, vision, vision]: text slice returns 2 txns
    on slice-pages 1-2, vision slice returns 2 txns on slice-pages 1-2.
    Merged statement must have 4 txns with source_page values 1, 2, 3, 4
    referencing the ORIGINAL PDF — that's the regulator-defense audit
    trail."""
    pdf_path = tmp_path / "mixed.pdf"
    _build_mixed_pdf(pdf_path)
    pdf_bytes = pdf_path.read_bytes()

    text_payload = {
        "summary": _summary(deposit_total="2000.00", withdrawal_total="0.00"),
        "transactions": [
            _txn("2026-01-01", "DEPOSIT A", "1000.00", 1, 1),
            _txn("2026-01-02", "DEPOSIT B", "1000.00", 2, 1),
        ],
        "synthetic_risk_indicators": ["INDICATOR_FROM_TEXT"],
    }
    vision_payload = {
        # The vision summary is discarded by the merger — only the
        # first group's summary is trusted, so this can be any shape.
        "summary": _summary(deposit_total="0.00", withdrawal_total="0.00"),
        "transactions": [
            _txn("2026-01-03", "DEPOSIT C", "1000.00", 1, 1),
            _txn("2026-01-04", "DEPOSIT D", "1000.00", 2, 1),
        ],
        "synthetic_risk_indicators": ["INDICATOR_FROM_VISION"],
    }
    llm = _CountedLLM(
        text_payloads=[text_payload],
        vision_payloads=[vision_payload],
    )
    decisions = [
        PageStrategyDecision(0, "text", 500, 80, 40),
        PageStrategyDecision(1, "text", 500, 80, 40),
        PageStrategyDecision(2, "vision", 5, 10, 70),
        PageStrategyDecision(3, "vision", 5, 10, 70),
    ]
    result = extract_statement_per_page(pdf_bytes, llm, decisions)

    assert llm.text_calls == 1, "consecutive text pages must merge into ONE LLM call"
    assert llm.vision_calls == 1, "consecutive vision pages must merge into ONE LLM call"

    assert isinstance(result.statement, ExtractedStatement)
    pages_seen = sorted(t.source_page for t in result.statement.transactions)
    # text slice pages 1,2 → original 1,2; vision slice pages 1,2 → original 3,4.
    assert pages_seen == [1, 2, 3, 4]

    # Summary is the FIRST group's summary (text in this layout).
    assert result.statement.summary.deposit_total == Decimal("2000.00")

    # synthetic_risk_indicators are UNIONED across both groups.
    assert set(result.synthetic_risk_indicators) == {
        "INDICATOR_FROM_TEXT",
        "INDICATOR_FROM_VISION",
    }


def test_per_page_truncation_propagates(tmp_path: Path) -> None:
    """If ANY sub-call's response was truncated by Bedrock, the merged
    result is marked truncated. Otherwise the validation gate would
    see a complete-looking but actually-truncated extraction."""
    pdf_path = tmp_path / "mixed.pdf"
    _build_mixed_pdf(pdf_path)
    pdf_bytes = pdf_path.read_bytes()

    text_payload = {
        "summary": _summary(deposit_total="1000.00", withdrawal_total="0.00"),
        "transactions": [_txn("2026-01-01", "DEP", "1000.00", 1, 1)],
        "synthetic_risk_indicators": [],
    }
    vision_payload = {
        "summary": _summary(deposit_total="0.00", withdrawal_total="0.00"),
        "transactions": [_txn("2026-01-03", "DEP2", "1000.00", 1, 1)],
        "synthetic_risk_indicators": [],
    }

    class _TruncatedVision(_CountedLLM):
        def extract_raw_json_from_images(
            self, page_images_png: list[bytes], prompt: str
        ) -> tuple[dict[str, Any], bool]:
            _ = (page_images_png, prompt)
            self.vision_calls += 1
            return self.vision_payloads.pop(0), True

    llm = _TruncatedVision(
        text_payloads=[text_payload],
        vision_payloads=[vision_payload],
    )
    decisions = [
        PageStrategyDecision(0, "text", 500, 80, 40),
        PageStrategyDecision(1, "vision", 5, 10, 70),
    ]
    result = extract_statement_per_page(pdf_bytes, llm, decisions)
    assert result.truncated is True


def test_per_page_rejects_empty_decisions() -> None:
    """Caller contract: ``decisions`` must be non-empty. Empty would
    mean classify_pages returned [] (corrupt PDF) — the pipeline
    shouldn't have called this function at all."""
    with pytest.raises(ExtractionError):
        extract_statement_per_page(b"x" * 100, _CountedLLM([], []), decisions=[])
