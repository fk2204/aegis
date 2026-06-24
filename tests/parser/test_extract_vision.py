"""Tests for the vision-pass extraction and the pipeline OCR branch."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from aegis.parser.extract import (
    ExtractionError,
    extract_statement,
    extract_statement_via_vision,
)
from aegis.parser.pipeline import MAX_OCR_PAGES, run_pipeline

# Re-uses the same canned LLM payload + stub class as the rest of the parser
# tests; the stub already implements both extract_raw_json (text path) and
# extract_raw_json_from_images (OCR fallback path).


def _write_image_only_pdf(path: Path, *, pages: int = 1) -> None:
    """Write a PDF whose pages contain only rasterised content."""
    text_src = pymupdf.open()
    page = text_src.new_page(width=612, height=792)
    page.insert_text(
        (72, 72),
        "Bank of Test\nDeposit 1234.56\nWithdrawal 500.00\n",
        fontsize=11,
    )
    pix = page.get_pixmap(dpi=150)
    text_src.close()

    out = pymupdf.open()
    for _ in range(pages):
        img_page = out.new_page(width=pix.width, height=pix.height)
        img_page.insert_image(img_page.rect, pixmap=pix)
    out.save(path)
    out.close()


def test_vision_extraction_shape_matches_text_extraction(
    clean_pdf_path: Path,
    clean_llm: Any,
) -> None:
    """OCR fallback must yield the same ExtractedStatement shape as the text path."""
    pdf_bytes = clean_pdf_path.read_bytes()

    text_result = extract_statement(pdf_bytes, clean_llm)
    vision_result = extract_statement_via_vision(pdf_bytes, clean_llm)

    assert text_result.statement.summary == vision_result.statement.summary
    assert len(text_result.statement.transactions) == len(vision_result.statement.transactions)
    for t_txn, v_txn in zip(
        text_result.statement.transactions,
        vision_result.statement.transactions,
        strict=True,
    ):
        assert t_txn.posted_date == v_txn.posted_date
        assert t_txn.amount == v_txn.amount
        assert t_txn.source_page == v_txn.source_page


def test_vision_extraction_rejects_empty_pdf(clean_llm: Any) -> None:
    with pytest.raises(ExtractionError):
        extract_statement_via_vision(b"", clean_llm)


def test_pipeline_routes_image_pdf_through_vision(
    tmp_path: Path,
    clean_llm: Any,
) -> None:
    """An image-only PDF must be parsed via the vision branch and reach
    proceed/review. Carries the ``[META] vision_routed: chars=N`` flag
    (the 2026-06-24 wire that distinguishes proactive image-only routing
    from the text→error→vision-fallback path; the latter still fires
    ``ocr_fallback_used``)."""
    pdf = tmp_path / "image_only.pdf"
    _write_image_only_pdf(pdf, pages=2)

    result = run_pipeline(str(pdf), clean_llm, today=date(2026, 2, 15))

    assert result.parse_status in {"proceed", "review"}, (
        f"image-only PDF unexpectedly went to manual_review; flags={result.all_flags}"
    )
    assert any("vision_routed" in f for f in result.all_flags), (
        f"expected vision_routed flag; got flags={result.all_flags}"
    )
    # The proactive vision route must NOT also fire ocr_fallback_used —
    # that flag is reserved for the text-extraction-error fallback path.
    assert not any("ocr_fallback_used" in f for f in result.all_flags), (
        f"vision_routed path must not also set ocr_fallback_used; got flags={result.all_flags}"
    )


def test_pipeline_oversize_image_pdf_lands_in_manual_review(
    tmp_path: Path,
    clean_llm: Any,
) -> None:
    """Image-only PDFs above MAX_OCR_PAGES must skip the LLM and land in manual_review."""
    pdf = tmp_path / "oversize_image.pdf"
    _write_image_only_pdf(pdf, pages=MAX_OCR_PAGES + 1)

    result = run_pipeline(str(pdf), clean_llm, today=date(2026, 2, 15))

    assert result.parse_status == "manual_review"
    assert result.extraction is None, "oversize image PDF must bail before calling the LLM"
    assert any("ocr_oversize_image_pdf" in f for f in result.validation.failures), (
        f"expected ocr_oversize_image_pdf failure; got {result.validation.failures}"
    )
