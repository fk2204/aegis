"""Tests for the text-layer probe used by the OCR fallback branch.

``_detect_text_layer`` is read by ``analyze_metadata`` and surfaces as
``MetadataAnalysis.has_text_layer``. The pipeline routes ``False`` results
through ``extract_statement_via_vision`` instead of the text-pass.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from aegis.parser.metadata import _detect_text_layer, analyze_metadata


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
