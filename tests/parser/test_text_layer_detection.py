"""Tests for the text-layer probe used by the OCR fallback branch.

``_probe_text_layer`` (and the back-compat boolean wrapper
``_detect_text_layer``) is read by ``analyze_metadata`` and surfaces as
``MetadataAnalysis.has_text_layer`` + ``MetadataAnalysis.text_layer_char_count``.
The pipeline routes a ``has_text_layer=False`` (i.e. ``chars < 50``)
result through ``extract_statement_via_vision`` instead of the text-pass,
with a ``[META] vision_routed: chars=N`` flag.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from aegis.parser.metadata import (
    _TEXT_LAYER_MIN_CHARS,
    _detect_text_layer,
    _probe_text_layer,
    analyze_metadata,
)


def _write_text_pdf(path: Path, text: str = "Statement page one\nDeposit 1234.56\n" * 20) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), text, fontsize=10)
    doc.save(path)
    doc.close()


def _write_image_only_pdf(path: Path) -> None:
    """Create a PDF whose pages contain ONLY rasterised content (no text layer).

    Strategy: render a real text page to a pixmap, then write that pixmap
    into a brand-new PDF as a page image. The resulting PDF has visible
    text but no extractable text layer — exactly the broker-PDF shape the
    OCR fallback is built for.
    """
    text_src = pymupdf.open()
    page = text_src.new_page(width=612, height=792)
    page.insert_text(
        (72, 72),
        "Bank of Test\nStatement period 2026-01-01 to 2026-01-31\n"
        "01/05 Deposit ABC 1,234.56\n01/12 Withdrawal XYZ 500.00\n",
        fontsize=11,
    )
    pix = page.get_pixmap(dpi=200)
    text_src.close()

    out = pymupdf.open()
    img_page = out.new_page(width=pix.width, height=pix.height)
    img_page.insert_image(img_page.rect, pixmap=pix)
    out.save(path)
    out.close()


def test_detect_text_layer_true_for_real_text_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "text.pdf"
    _write_text_pdf(pdf)
    assert _detect_text_layer(pdf) is True


def test_detect_text_layer_false_for_image_only_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "image_only.pdf"
    _write_image_only_pdf(pdf)
    assert _detect_text_layer(pdf) is False


def test_analyze_metadata_surfaces_has_text_layer(tmp_path: Path) -> None:
    text_pdf = tmp_path / "text.pdf"
    image_pdf = tmp_path / "image_only.pdf"
    _write_text_pdf(text_pdf)
    _write_image_only_pdf(image_pdf)

    assert analyze_metadata(text_pdf).has_text_layer is True
    assert analyze_metadata(image_pdf).has_text_layer is False


def test_detect_text_layer_defaults_true_on_corrupt_pdf(tmp_path: Path) -> None:
    """An unreadable PDF must default to has_text_layer=True so the text path
    runs and surfaces the real error — never silently routed to vision."""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not a pdf at all")
    assert _detect_text_layer(bad) is True


# ---------------------------------------------------------------------
# Aggregate-total semantics (2026-06-24 vision-routing wire)
# ---------------------------------------------------------------------
#
# The detection threshold is the SUM of non-whitespace chars across the
# first ``_TEXT_LAYER_PROBE_PAGES`` pages (3 today). Previously the
# check returned True as soon as any single page crossed the floor —
# which let a PDF with a 60-char header watermark on page 1 and pure
# images on pages 2-3 ride the text path and waste a Bedrock extraction
# call before the error-fallback re-routed to vision. Aggregate
# semantics catch that case at metadata time.


def _write_pdf_with_chars(path: Path, *, page_count: int, chars_per_page: int) -> None:
    """Build a PDF with exactly ``chars_per_page`` non-whitespace chars
    on each page. Used to pin the aggregate-vs-per-page distinction in
    a deterministic way."""
    doc = pymupdf.open()
    body = "a" * chars_per_page if chars_per_page > 0 else ""
    for _ in range(page_count):
        page = doc.new_page(width=612, height=792)
        if body:
            page.insert_text((72, 72), body, fontsize=10)
    doc.save(path)
    doc.close()


def test_probe_text_layer_returns_total_chars_across_probed_pages(
    tmp_path: Path,
) -> None:
    """``_probe_text_layer`` reports the SUM of chars across the first
    3 pages — not just the first page that hit the floor. Pin the
    contract so a future refactor doesn't silently revert to per-page."""
    pdf = tmp_path / "three_pages_30_each.pdf"
    _write_pdf_with_chars(pdf, page_count=3, chars_per_page=30)
    has_text_layer, total = _probe_text_layer(pdf)
    # 3 pages * 30 chars = 90 total ≥ 50 floor → text path.
    assert total == 90
    assert has_text_layer is True


def test_aggregate_total_below_floor_routes_to_vision(tmp_path: Path) -> None:
    """A PDF with 10 chars total across all probed pages is below the
    50-char floor → has_text_layer=False, vision-first routing."""
    pdf = tmp_path / "tiny_text.pdf"
    _write_pdf_with_chars(pdf, page_count=3, chars_per_page=3)  # 9 total
    has_text_layer, total = _probe_text_layer(pdf)
    assert total == 9
    assert has_text_layer is False


def test_aggregate_total_above_floor_routes_to_text(tmp_path: Path) -> None:
    """A PDF with text well above the 50-char floor → has_text_layer=True,
    text path (the operator's spec ``200 chars → text path`` case;
    the exact extracted count depends on pymupdf's page-width wrapping
    so we assert the floor is comfortably cleared, not an exact total)."""
    pdf = tmp_path / "above_floor.pdf"
    _write_pdf_with_chars(pdf, page_count=3, chars_per_page=200)
    has_text_layer, total = _probe_text_layer(pdf)
    assert total > _TEXT_LAYER_MIN_CHARS
    assert has_text_layer is True


def test_aggregate_strictly_at_threshold_passes(tmp_path: Path) -> None:
    """``total >= _TEXT_LAYER_MIN_CHARS`` — exact-threshold equality
    passes (not >). Locks the boundary so a future refactor that flips
    to strict-> is caught."""
    pdf = tmp_path / "exact_threshold.pdf"
    _write_pdf_with_chars(pdf, page_count=1, chars_per_page=_TEXT_LAYER_MIN_CHARS)
    has_text_layer, total = _probe_text_layer(pdf)
    assert total == _TEXT_LAYER_MIN_CHARS
    assert has_text_layer is True


def test_metadata_exposes_text_layer_char_count(tmp_path: Path) -> None:
    """``MetadataAnalysis.text_layer_char_count`` carries the sum so the
    pipeline can log it on the ``[META] vision_routed: chars=N`` line
    without re-probing the PDF."""
    image_pdf = tmp_path / "image_only.pdf"
    _write_image_only_pdf(image_pdf)
    meta = analyze_metadata(image_pdf)
    assert meta.has_text_layer is False
    # Image-only PDF probes < 50 chars on the rasterised pages.
    assert meta.text_layer_char_count < _TEXT_LAYER_MIN_CHARS

    text_pdf = tmp_path / "text.pdf"
    _write_text_pdf(text_pdf)
    meta_text = analyze_metadata(text_pdf)
    assert meta_text.has_text_layer is True
    assert meta_text.text_layer_char_count >= _TEXT_LAYER_MIN_CHARS
