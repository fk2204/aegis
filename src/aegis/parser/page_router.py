"""Per-page text/vision auto-escalation classifier (mp Phase 6.5).

For each page in the PDF, decides whether the text-layer extraction
path or the vision OCR path is the better fit. The pipeline uses these
decisions to route mixed PDFs (text-bearing cover + scanned detail
pages, or vice versa) without sending the whole document down one
path. Text-on-text-pages is ~5-8x cheaper in Bedrock tokens than
vision-on-text-pages, so picking the right strategy per-page is a
direct token-cost win for hybrid statements.

The classifier is **deterministic and pure**: same PDF in, same
decisions out. It does NOT call the LLM — strategy is determined by
text-layer inspection alone.

Hard requirement (master plan §6.5): if any page has low confidence
in BOTH strategies, the pipeline must route the whole doc to
manual_review. Extracting one page poorly is worse than not
extracting it; aggregate metrics are the load-bearing output and
they cannot be reconstructed from a partial transaction list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import pymupdf

from aegis.logger import get_logger

_log = get_logger(__name__)


PageStrategy = Literal["text", "vision"]


# Per-page text density threshold. Below this, the page goes to vision
# OCR even if it has SOME extractable text (e.g. just a watermark or
# page number). Tuned against the existing parser corpus — real bank
# pages yield 200-2000+ non-whitespace chars; image pages yield 0-30.
TEXT_DENSITY_THRESHOLD: Final[int] = 100

# Confidence floor below which a strategy is "not trusted." When BOTH
# strategies fall below this floor on the same page, the pipeline
# routes the document to manual_review (fail-closed per the plan).
CONFIDENCE_FLOOR: Final[int] = 30


@dataclass(frozen=True)
class PageStrategyDecision:
    """One page's routing decision + reasoning.

    ``text_confidence`` and ``vision_confidence`` are 0-100 estimates
    of how well each strategy would handle this page. ``strategy`` is
    the higher-confidence option. The OTHER value matters because the
    pipeline reads both to detect "neither strategy is confident
    enough" → whole-doc manual_review.

    Frozen + slots-free dataclass: this is part of a structured log
    payload that gets compared in tests, mutation is unintentional.
    """

    page_index: int  # 0-indexed within the source PDF
    strategy: PageStrategy
    text_density: int  # non-whitespace char count on the page
    text_confidence: int  # 0-100
    vision_confidence: int  # 0-100


def classify_pages(pdf_path: str | Path) -> list[PageStrategyDecision]:
    """Classify each page's preferred extraction strategy.

    Returns one ``PageStrategyDecision`` per page in document order.
    On pymupdf failure, returns an empty list so the caller can fall
    back to legacy whole-doc routing (which is conservative — better
    to OCR a text-bearing PDF on fallback than to silently route a
    text-only doc to a vision path that would also work).
    """
    decisions: list[PageStrategyDecision] = []
    try:
        with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
            for i in range(doc.page_count):
                text = doc.load_page(i).get_text("text") or ""
                density = sum(1 for c in text if not c.isspace())
                decisions.append(_decide(i, density))
    except Exception:
        _log.warning(
            "parser.page_router.classify_failed", extra={"pdf_path": str(pdf_path)}
        )
        return []
    return decisions


def _decide(page_index: int, text_density: int) -> PageStrategyDecision:
    """Pure scoring: text density → strategy + per-strategy confidence.

    The scoring shape:
      - Density ≥ threshold → ``text`` strategy with confidence scaled
        by density (more text = higher text confidence). Vision is
        still listed at a moderate ~40 because vision CAN read text
        pages too; it's just wasteful.
      - Density < threshold → ``vision`` strategy. Confidence is
        higher when density is near-zero (clear image-only page) and
        lower when density is in the 10-99 ambiguous band where the
        page might be a sparse text page that vision will still handle
        but text-extraction might've handled too.
    """
    if text_density >= TEXT_DENSITY_THRESHOLD:
        # Text-bearing page. Confidence climbs gently with density,
        # capped at 100 so very dense pages don't blow out the scale.
        text_conf = min(100, 60 + text_density // 50)
        return PageStrategyDecision(
            page_index=page_index,
            strategy="text",
            text_density=text_density,
            text_confidence=text_conf,
            vision_confidence=40,
        )
    # Sparse / no text: vision wins.
    if text_density < 10:
        # Effectively zero text — high vision confidence, near-zero
        # text confidence. The clean image-only page case.
        return PageStrategyDecision(
            page_index=page_index,
            strategy="vision",
            text_density=text_density,
            text_confidence=text_density * 2,
            vision_confidence=70,
        )
    # 10 ≤ density < threshold: ambiguous band. Both strategies are
    # plausible; vision is favored, but with lower confidence to mark
    # the uncertainty.
    return PageStrategyDecision(
        page_index=page_index,
        strategy="vision",
        text_density=text_density,
        text_confidence=20 + text_density // 4,
        vision_confidence=50,
    )


def summarize(decisions: list[PageStrategyDecision]) -> dict[str, int]:
    """Per-document roll-up used by the pipeline's structured log.

    Returned shape is JSON-safe (ints only). Empty input → all zeros so
    the log format is stable when the classifier fell back.
    """
    if not decisions:
        return {
            "page_count": 0,
            "text_pages": 0,
            "vision_pages": 0,
            "low_confidence_pages": 0,
        }
    return {
        "page_count": len(decisions),
        "text_pages": sum(1 for d in decisions if d.strategy == "text"),
        "vision_pages": sum(1 for d in decisions if d.strategy == "vision"),
        "low_confidence_pages": sum(1 for d in decisions if _is_low_confidence(d)),
    }


def has_low_confidence(decisions: list[PageStrategyDecision]) -> bool:
    """True if any page has BOTH strategies below ``CONFIDENCE_FLOOR``.

    The pipeline reads this directly: when True, the document is
    routed to manual_review with reason code
    ``page_router_low_confidence``. Partial extraction is worse than
    no extraction here — the downstream aggregates are load-bearing.
    """
    return any(_is_low_confidence(d) for d in decisions)


def _is_low_confidence(decision: PageStrategyDecision) -> bool:
    return (
        decision.text_confidence < CONFIDENCE_FLOOR
        and decision.vision_confidence < CONFIDENCE_FLOOR
    )


def is_homogeneous(decisions: list[PageStrategyDecision]) -> PageStrategy | None:
    """If every page chose the same strategy, return it; otherwise None.

    The pipeline uses this to short-circuit: a homogeneous doc skips
    the per-page extraction merge entirely and runs the original
    whole-doc extractor for that strategy. Mixed docs take the
    per-page path. Empty input → None (caller falls back to legacy).
    """
    if not decisions:
        return None
    first = decisions[0].strategy
    return first if all(d.strategy == first for d in decisions) else None


__all__ = [
    "CONFIDENCE_FLOOR",
    "TEXT_DENSITY_THRESHOLD",
    "PageStrategy",
    "PageStrategyDecision",
    "classify_pages",
    "has_low_confidence",
    "is_homogeneous",
    "summarize",
]
