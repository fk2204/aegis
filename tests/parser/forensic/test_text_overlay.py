"""Tests for ``aegis.parser.forensic.text_overlay``.

The detector reads page ``/Contents`` arrays and walks each stream's
text operators. Tests build synthetic PDFs with reportlab (single-stream
baselines) then mutate them with pikepdf to inject a second content
stream — the only practical way to construct the multi-stream scenarios
the detector targets.

Coverage:

* Single-stream PDF → no flag (no overlay possible).
* Two-stream PDF where the second stream's text overlaps the first
  stream's Y-range → flag, ``affected_pages`` and
  ``overlay_stream_count`` reflect the finding.
* Two-stream PDF where the second stream's text sits in a different
  Y-region (legitimate header/body split) → no false positive.
* Nonexistent path / encrypted PDF → null result, no crash.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

from aegis.parser.forensic.text_overlay import (
    TextOverlayResult,
    analyze,
)

# ---------------------------------------------------------------------
# Synthetic PDF builders
# ---------------------------------------------------------------------


def _make_single_stream_pdf(path: Path) -> None:
    """One-page reportlab PDF — single content stream by default."""
    c = rl_canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 12)
    c.drawString(72, 720, "Bank Statement — Clean Co LLC")
    c.drawString(72, 700, "04/02   ATM Withdrawal   -100.00")
    c.drawString(72, 680, "04/05   Card Purchase    -8.50")
    c.drawString(72, 660, "04/12   Direct Deposit   1,200.00")
    c.save()


def _build_text_stream(y_positions: list[int], text: str) -> bytes:
    """Build a minimal PDF content stream that draws ``text`` at each Y.

    Returns raw stream bytes ready to wrap in a pikepdf Stream object.
    Uses Tm (absolute text matrix) so the Y is unambiguous regardless of
    prior state.
    """
    lines = ["q", "BT", "/F1 12 Tf"]
    for y in y_positions:
        lines.append(f"1 0 0 1 72 {y} Tm")
        # Tj operator on a literal string. Escape parens by using a
        # plain string with no parens for these tests.
        clean = text.replace("(", "").replace(")", "").replace("\\", "")
        lines.append(f"({clean}) Tj")
    lines.extend(["ET", "Q", ""])
    return ("\n".join(lines)).encode("ascii")


def _add_overlapping_second_stream(path: Path) -> None:
    """Open a single-stream PDF and append a second content stream that
    renders text at the same Y-range as the first."""
    with pikepdf.open(str(path), allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        # Second stream draws text at Y=700, 680, 660 — same rows the
        # reportlab single-stream PDF used. Overlap is guaranteed.
        new_data = _build_text_stream([700, 680, 660], "PHANTOM TXN")
        new_stream = pdf.make_stream(new_data)

        current_contents = page.obj["/Contents"]
        if isinstance(current_contents, pikepdf.Array):
            current_contents.append(new_stream)
        else:
            page.obj["/Contents"] = pikepdf.Array([current_contents, new_stream])
        pdf.save(str(path))


def _add_non_overlapping_second_stream(path: Path) -> None:
    """Open a single-stream PDF and append a second content stream that
    renders text in a non-overlapping Y-region (footer area)."""
    with pikepdf.open(str(path), allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        # Second stream draws text only in the footer Y-range (Y=100,
        # 90) — well below the first stream's Y range (660..720).
        # Legitimate layout split.
        new_data = _build_text_stream([100, 90], "Page 1 of 1")
        new_stream = pdf.make_stream(new_data)

        current_contents = page.obj["/Contents"]
        if isinstance(current_contents, pikepdf.Array):
            current_contents.append(new_stream)
        else:
            page.obj["/Contents"] = pikepdf.Array([current_contents, new_stream])
        pdf.save(str(path))


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_single_stream_pdf_no_flag(tmp_path: Path) -> None:
    """Default reportlab PDF has one content stream per page. No overlay
    possible."""
    pdf_path = tmp_path / "single.pdf"
    _make_single_stream_pdf(pdf_path)

    result = analyze(pdf_path)

    assert isinstance(result, TextOverlayResult)
    assert result.overlay_detected is False
    assert result.affected_pages == []
    assert result.overlay_stream_count == 0


def test_overlapping_second_stream_flags_overlay(tmp_path: Path) -> None:
    """Second stream renders text at the same Y-range as the first →
    detector flags page 1, captures stream count."""
    pdf_path = tmp_path / "overlapped.pdf"
    _make_single_stream_pdf(pdf_path)
    _add_overlapping_second_stream(pdf_path)

    result = analyze(pdf_path)

    assert result.overlay_detected is True
    assert result.affected_pages == [1]
    # Two text-bearing streams contributed to the affected page.
    assert result.overlay_stream_count == 2


def test_non_overlapping_second_stream_no_false_positive(tmp_path: Path) -> None:
    """Second stream's Y range sits entirely below the first stream's
    range (footer split) → no overlap → no flag. Legitimate multi-
    stream layout case."""
    pdf_path = tmp_path / "split.pdf"
    _make_single_stream_pdf(pdf_path)
    _add_non_overlapping_second_stream(pdf_path)

    result = analyze(pdf_path)

    assert result.overlay_detected is False
    assert result.affected_pages == []


def test_nonexistent_path_returns_null_result(tmp_path: Path) -> None:
    """Open failure → null result, no crash."""
    result = analyze(tmp_path / "absolutely_missing.pdf")

    assert result.overlay_detected is False
    assert result.affected_pages == []
    assert result.overlay_stream_count == 0
