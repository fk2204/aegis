"""Tests for the SHADOW-MODE v2 text-layer probe.

``_probe_text_layer_v2_shadow`` is a stricter heuristic that asks
"does this PDF's text layer look like REAL transaction data?" via a
per-page character floor AND a numeric-line floor. It is NOT live-
gated — the parser pipeline emits a ``[SHADOW]
text_layer_probe_v2_disagrees: ...`` flag onto ``all_flags`` whenever
the v2 verdict differs from the live ``has_text_layer`` routing
decision, so the operator can validate the disagreement set on a
corpus before flipping the routing gate.

Per CLAUDE.md "Decision-boundary changes — deliberate + shadow-first":
the v2 probe MUST NOT change live routing. These tests pin the probe's
contract (return shape, debug payload, threshold semantics) so a future
refactor can't silently drift the heuristic before the live flip.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from aegis.parser.metadata import (
    _V2_MIN_CHARS_PER_PAGE,
    _V2_MIN_NUMERIC_LINES,
    _probe_text_layer_v2_shadow,
)


def _write_rich_text_pdf(path: Path) -> None:
    """Build a PDF with hundreds of chars per page AND many numeric
    transaction-shaped lines. Mirrors a real bank statement: each line
    carries a date + description + dollar amount with a decimal cent."""
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=612, height=792)
        body_lines = []
        for i in range(20):
            # Mix of currency-with-$, comma-grouped, and decimal-cent
            # forms so the numeric pattern matches multiple shapes.
            day = (i % 28) + 1
            amount_dollars = 1234 + i
            amount_cents = (i * 7) % 100
            body_lines.append(
                f"01/{day:02d} Deposit ABC Co transfer ${amount_dollars},{i:03d}.{amount_cents:02d}"
            )
        page.insert_text((36, 36), "\n".join(body_lines), fontsize=10)
    doc.save(path)
    doc.close()


def _write_watermark_only_pdf(path: Path) -> None:
    """Build a PDF whose pages contain ONLY a short brand watermark
    (~30 chars per page) with zero numeric / transaction content. This
    mirrors the misclassification class the v2 probe targets: the live
    50-char aggregate floor counts 3 * 30 = 90 chars and routes to
    text, but there's no actual transaction data to extract."""
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=612, height=792)
        # ~30 chars, no digits, no currency symbols.
        page.insert_text((72, 72), "Arthur State Bank watermark", fontsize=11)
    doc.save(path)
    doc.close()


def _write_image_only_pdf(path: Path) -> None:
    """Build a PDF whose pages contain ONLY rasterised content (no
    text layer at all). Same generator pattern as
    ``test_text_layer_detection._write_image_only_pdf``."""
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


def test_v2_probe_passes_rich_text_pdf(tmp_path: Path) -> None:
    """A PDF with rich text + 20 numeric transaction lines per page
    routes to TEXT (would_route_to_vision = False)."""
    pdf = tmp_path / "rich_text.pdf"
    _write_rich_text_pdf(pdf)
    would_route_to_vision, debug = _probe_text_layer_v2_shadow(pdf)
    assert would_route_to_vision is False
    assert debug["reason"] == "passes_v2_floor"
    chars_avg = debug["chars_per_page_avg"]
    numeric_lines = debug["numeric_lines"]
    assert isinstance(chars_avg, float)
    assert isinstance(numeric_lines, int)
    assert chars_avg >= _V2_MIN_CHARS_PER_PAGE
    assert numeric_lines >= _V2_MIN_NUMERIC_LINES


def test_v2_probe_routes_watermark_only_pdf_to_vision(tmp_path: Path) -> None:
    """A PDF with only a short brand watermark on each page routes to
    VISION (would_route_to_vision = True) — exactly the
    misclassification class the v2 probe is designed to catch."""
    pdf = tmp_path / "watermark_only.pdf"
    _write_watermark_only_pdf(pdf)
    would_route_to_vision, debug = _probe_text_layer_v2_shadow(pdf)
    assert would_route_to_vision is True
    # Numeric-line count is the failure mode here: the watermark has no
    # digits/currency. Chars-per-page may or may not be below 150
    # depending on watermark length; the assertion that locks the
    # contract is the numeric-line floor.
    numeric_lines = debug["numeric_lines"]
    assert isinstance(numeric_lines, int)
    assert numeric_lines < _V2_MIN_NUMERIC_LINES
    assert debug["reason"] in {"low_chars_per_page", "low_numeric_lines"}


def test_v2_probe_routes_image_only_pdf_to_vision(tmp_path: Path) -> None:
    """A PDF with zero text layer (rasterised content) routes to
    VISION (would_route_to_vision = True). Same verdict as the live
    probe on this class of PDF."""
    pdf = tmp_path / "image_only.pdf"
    _write_image_only_pdf(pdf)
    would_route_to_vision, debug = _probe_text_layer_v2_shadow(pdf)
    assert would_route_to_vision is True
    # Image-only PDF has effectively zero extractable text on the
    # rasterised page → both v2 floors fail. Char-floor fires first
    # given the verdict order (chars_per_page checked before
    # numeric_lines), so the reason is low_chars_per_page.
    assert debug["reason"] == "low_chars_per_page"
    chars_avg = debug["chars_per_page_avg"]
    assert isinstance(chars_avg, float)
    assert chars_avg < _V2_MIN_CHARS_PER_PAGE


def test_v2_probe_defaults_to_route_vision_on_corrupt_pdf(tmp_path: Path) -> None:
    """Unreadable PDF defaults to ``(True, {...})`` — conservative
    posture for a shadow probe (lean toward emitting a disagreement
    flag when we can't measure, rather than masking the problem). The
    debug payload still carries the "probe_failure" reason so the
    operator can see what happened."""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not a pdf at all")
    would_route_to_vision, debug = _probe_text_layer_v2_shadow(bad)
    assert would_route_to_vision is True
    assert debug["reason"] == "probe_failure"


def test_v2_probe_debug_payload_shape(tmp_path: Path) -> None:
    """Debug dict carries the three contract keys regardless of the
    verdict path. Pin the shape so downstream callers (the pipeline
    flag emitter, future dossier UI) don't break on a missing key."""
    pdf = tmp_path / "rich_text.pdf"
    _write_rich_text_pdf(pdf)
    _, debug = _probe_text_layer_v2_shadow(pdf)
    assert set(debug.keys()) == {"chars_per_page_avg", "numeric_lines", "reason"}
