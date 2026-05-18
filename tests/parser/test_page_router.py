"""Per-page text/vision auto-escalation classifier tests (mp Phase 6.5).

Covers ``aegis.parser.page_router``:

- ``classify_pages`` scores each page; high text density → ``text``,
  near-zero text density → ``vision`` with high confidence.
- ``is_homogeneous`` returns the single strategy when all pages agree.
- ``summarize`` produces a JSON-safe roll-up for the pipeline log.
- ``has_low_confidence`` is True when both strategies fall below the
  floor on any one page — the fail-closed gate.
- Corrupt PDFs return ``[]`` (caller falls back to legacy routing).

Two PDF shapes recycled from test_text_layer_detection.py:
- text page: ``page.insert_text(...)`` — high text density.
- image page: render text → pixmap → embed as image (no text layer).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from aegis.parser.page_router import (
    CONFIDENCE_FLOOR,
    TEXT_DENSITY_THRESHOLD,
    PageStrategyDecision,
    classify_pages,
    has_low_confidence,
    is_homogeneous,
    summarize,
)

# ---------------------------------------------------------------------------
# PDF builders (small, deterministic). pymupdf has no type stubs; the
# test helpers fall back to Any for parameter typing.
# ---------------------------------------------------------------------------


def _write_text_page(doc: Any, lines: int = 30) -> None:
    """Append a page with `lines` text rows so density crosses the threshold."""
    page = doc.new_page(width=612, height=792)
    body = "\n".join(
        f"01/{i:02d}/2026  DEPOSIT ACH FROM XYZ INC  $1,234.56  Balance $99,999.99"
        for i in range(1, lines + 1)
    )
    page.insert_text((72, 72), body, fontsize=9)


def _write_image_only_page(doc: Any) -> None:
    """Append a page whose visible content is rasterized (no text layer)."""
    text_src = pymupdf.open()
    src = text_src.new_page(width=612, height=792)
    src.insert_text(
        (72, 72),
        "Bank of Test\nStatement period 2026-01-01 to 2026-01-31\n",
        fontsize=11,
    )
    pix = src.get_pixmap(dpi=200)
    text_src.close()
    img_page = doc.new_page(width=pix.width, height=pix.height)
    img_page.insert_image(img_page.rect, pixmap=pix)


def _write_pdf(path: Path, *, pages: list[str]) -> None:
    """Build a multi-page PDF where each element of ``pages`` is 'text' or 'image'."""
    out = pymupdf.open()
    for kind in pages:
        if kind == "text":
            _write_text_page(out)
        elif kind == "image":
            _write_image_only_page(out)
        else:  # pragma: no cover — test-internal assertion
            raise ValueError(f"unknown page kind: {kind}")
    out.save(path)
    out.close()


# ---------------------------------------------------------------------------
# Pure scoring (no PDF)
# ---------------------------------------------------------------------------


def test_summarize_handles_empty_decisions() -> None:
    """Empty decision list → stable-shape zeros so the pipeline log
    format doesn't vary based on classifier success/failure."""
    assert summarize([]) == {
        "page_count": 0,
        "text_pages": 0,
        "vision_pages": 0,
        "low_confidence_pages": 0,
    }


def test_is_homogeneous_empty_returns_none() -> None:
    assert is_homogeneous([]) is None


def test_is_homogeneous_all_text() -> None:
    decisions = [
        PageStrategyDecision(0, "text", 500, 80, 40),
        PageStrategyDecision(1, "text", 800, 90, 40),
    ]
    assert is_homogeneous(decisions) == "text"


def test_is_homogeneous_mixed_returns_none() -> None:
    decisions = [
        PageStrategyDecision(0, "text", 500, 80, 40),
        PageStrategyDecision(1, "vision", 5, 10, 70),
    ]
    assert is_homogeneous(decisions) is None


def test_has_low_confidence_when_both_below_floor() -> None:
    decisions = [
        PageStrategyDecision(0, "vision", 50, 20, 20),  # both < floor=30
    ]
    assert has_low_confidence(decisions) is True


def test_has_low_confidence_false_when_one_strategy_above_floor() -> None:
    decisions = [
        PageStrategyDecision(0, "vision", 50, 20, 50),
    ]
    assert has_low_confidence(decisions) is False


# ---------------------------------------------------------------------------
# Real-PDF classification
# ---------------------------------------------------------------------------


def test_classify_text_only_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "text_only.pdf"
    _write_pdf(pdf, pages=["text", "text"])
    decisions = classify_pages(pdf)
    assert len(decisions) == 2
    for d in decisions:
        assert d.strategy == "text"
        assert d.text_density >= TEXT_DENSITY_THRESHOLD
        assert d.text_confidence >= CONFIDENCE_FLOOR
    assert is_homogeneous(decisions) == "text"
    assert has_low_confidence(decisions) is False


def test_classify_image_only_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "image_only.pdf"
    _write_pdf(pdf, pages=["image", "image"])
    decisions = classify_pages(pdf)
    assert len(decisions) == 2
    for d in decisions:
        assert d.strategy == "vision"
        assert d.text_density < TEXT_DENSITY_THRESHOLD
        assert d.vision_confidence >= CONFIDENCE_FLOOR
    assert is_homogeneous(decisions) == "vision"
    assert has_low_confidence(decisions) is False


def test_classify_mixed_pdf_routes_per_page(tmp_path: Path) -> None:
    """Hybrid statement: page 1 text-bearing, page 2 image. Per-page
    routing should pick text for page 1 and vision for page 2 — this
    is the case the feature is built for."""
    pdf = tmp_path / "mixed.pdf"
    _write_pdf(pdf, pages=["text", "image"])
    decisions = classify_pages(pdf)
    assert len(decisions) == 2
    assert decisions[0].strategy == "text"
    assert decisions[1].strategy == "vision"
    assert is_homogeneous(decisions) is None  # mixed
    rollup = summarize(decisions)
    assert rollup == {
        "page_count": 2,
        "text_pages": 1,
        "vision_pages": 1,
        "low_confidence_pages": 0,
    }


def test_classify_returns_empty_list_on_corrupt_pdf(tmp_path: Path) -> None:
    """Pymupdf failure must NOT crash the classifier; an empty list lets
    the pipeline fall back to legacy whole-doc routing without losing
    the document."""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not a pdf at all")
    assert classify_pages(bad) == []


def test_classify_preserves_page_order(tmp_path: Path) -> None:
    """``page_index`` must be 0-indexed and match document order. The
    extract layer relies on this ordering to rebuild the source_page
    remap correctly."""
    pdf = tmp_path / "ordered.pdf"
    _write_pdf(pdf, pages=["text", "image", "text", "image"])
    decisions = classify_pages(pdf)
    assert [d.page_index for d in decisions] == [0, 1, 2, 3]
    assert [d.strategy for d in decisions] == ["text", "vision", "text", "vision"]
