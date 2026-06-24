"""Row-level font consistency detector.

Catches paste-over fraud where transaction rows are added in a different
tool than the rest of the statement was rendered with. Complementary to
``aegis.parser.metadata._font_inconsistency`` (which is PAGE-level — it
flags when a whole page's font set has no overlap with sibling pages).

The detector operates per-page:

1. Extract every text span with ``(text, font, size, bbox)`` via
   ``pymupdf`` (the per-span font + size is the cleanest path; pikepdf
   exposes per-page font names but not per-text-run font assignment).
2. Classify each span as "transaction-like" or "non-transaction" by
   heuristic — the metadata pass runs BEFORE Bedrock extraction so we
   don't have a structured transaction list to consult. Currency- or
   date-bearing spans become transaction-like; everything else (titles,
   summary block, footers) becomes non-transaction.
3. Compute the modal ``(font, size)`` profile of the non-transaction
   spans on that page — that's the page's baseline "voice."
4. Flag the page as inconsistent when ANY of:
     * Some transaction span uses a font family not present in
       non-transaction spans on the same page.
     * Some transaction span uses a size > 1pt off the modal size.
     * More than 20% of transaction spans have a font profile that
       differs from the modal.

The result rolls up to a document-level ``FontConsistencyResult`` with
the count of affected pages, the modal font seen, the anomalous fonts
seen in transaction rows, and a confidence number derived from the
affected-page fraction.

Failure modes deliberately fall back to no-flag:
  * No extractable text (image-only PDFs) → ``inconsistency_detected=False``,
    affected_page_count=0. Vision-mode parsing handles those documents,
    not this detector.
  * Only one of (transaction-like, non-transaction) spans found on a
    page → skip the page (can't compare).
  * ``pymupdf`` raises during open / page walk → ``inconsistency_detected=False``,
    affected_page_count=0. Mirrors the existing ``_font_inconsistency``
    error posture in ``metadata.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import fitz  # pymupdf — per-span font + size access; already in pyproject deps.

# ─────────────────────────────────────────────────────────────────────
# Heuristics for transaction-like text
# ─────────────────────────────────────────────────────────────────────


# Currency pattern: optional $/-, optional thousands grouping, 2-decimal
# cents. Matches "$1,234.56", "-12.34", "0.00", "12,345.67".
_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(r"-?\$?\d{1,3}(?:,\d{3})*\.\d{2}")

# Date pattern: MM/DD or MM-DD with 1-2 digit month/day. Bank statements
# almost universally use these abbreviations in transaction rows.
_DATE_RE: Final[re.Pattern[str]] = re.compile(r"\b\d{1,2}[/\-]\d{1,2}\b")

# Size delta above which we treat the font as materially different (per
# operator spec). ≤1pt deltas are routine between font hinting / kerning
# rounding and don't indicate paste-over.
_SIZE_DELTA_THRESHOLD: Final[float] = 1.0

# Per-page differing-fraction threshold (per operator spec): >20% of
# transaction spans needing to disagree before the page itself counts
# as inconsistent on the differing-fraction branch.
_DIFFERING_PCT_THRESHOLD: Final[float] = 0.20


def _looks_like_transaction(text: str) -> bool:
    """True when a text span likely belongs to a transaction row.

    Currency-bearing spans dominate transaction tables; date-bearing
    spans cover the per-row date column in layouts where the date and
    amount land in separate spans. Either match is enough — over-
    classifying as transaction is preferable to missing real
    transaction rows, since the comparison is against a modal that's
    expected to be stable.
    """
    if _CURRENCY_RE.search(text):
        return True
    if _DATE_RE.search(text):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Result shape
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FontConsistencyResult:
    """Document-level rollup of per-page row-level font inconsistencies.

    * ``inconsistency_detected`` is the gate the caller in ``metadata.py``
      reads to decide whether to surface the ``[META]
      font_inconsistency_detected`` flag.
    * ``affected_page_count`` is the count of pages where the per-page
      gate fired (any one of the three conditions in this module's
      docstring).
    * ``modal_font`` is the (font, size) string from the first page
      where a modal was determinable, captured for operator
      diagnostics — NOT used in scoring.
    * ``anomalous_fonts`` lists every font family seen in transaction
      spans that did NOT appear in non-transaction spans on the same
      page (the strongest of the three signals). Sorted, de-duplicated.
    * ``confidence`` is a 0..1 number derived from the affected-page
      fraction. Not a probabilistic estimate — just an operator-facing
      cue that "1/12 pages affected" is weaker than "8/12 pages
      affected."
    """

    inconsistency_detected: bool
    affected_page_count: int
    modal_font: str
    anomalous_fonts: list[str] = field(default_factory=list)
    confidence: float = 0.0


# Conservative no-signal default used by every failure path.
_NULL_RESULT: Final[FontConsistencyResult] = FontConsistencyResult(
    inconsistency_detected=False,
    affected_page_count=0,
    modal_font="",
    anomalous_fonts=[],
    confidence=0.0,
)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def analyze(pdf_path: str | Path) -> FontConsistencyResult:
    """Run the row-level font consistency check across every page.

    Conservative fallback: any failure to open / read the PDF returns
    the null result (no flag, no score contribution). This mirrors the
    existing ``metadata._font_inconsistency`` posture — we never want a
    forensic-detector error to fail-closed on a legitimate statement.

    Per-page logic:

    1. ``modal_family`` = the most common font family in non-transaction
       spans. This is the page's "voice" independent of heading vs body
       size variance.
    2. ``body_size`` = the single shared size of non-transaction spans
       WHEN they all share a size. If non-tx spans have multiple sizes
       (headings + summary block on the same page), the size signal is
       too noisy to be reliable — body_size is set to ``None`` and the
       size check is skipped for the page.
    3. Page is flagged when ANY of these hold:
         * Some transaction span uses a font family not present in any
           non-transaction span on that page (strongest signal — direct
           evidence of paste-over with a different rendering tool).
         * ``body_size`` is well-defined AND some transaction span's
           size differs by more than ``_SIZE_DELTA_THRESHOLD`` from it.
         * ``body_size`` is well-defined AND more than
           ``_DIFFERING_PCT_THRESHOLD`` of transaction spans have a
           "differing profile" (different family OR significant size
           delta from body_size).
    """
    path = Path(pdf_path)
    try:
        doc = fitz.open(str(path))
    except Exception:
        return _NULL_RESULT

    try:
        affected_pages = 0
        anomalous_fonts: set[str] = set()
        modal_seen: str = ""
        total_pages = doc.page_count

        for page_index in range(total_pages):
            try:
                page = doc.load_page(page_index)
                text_dict = page.get_text("dict")
            except Exception:  # noqa: S112 — per-page failures are best-effort
                continue

            spans = _collect_spans(text_dict)
            if not spans:
                continue

            tx_spans = [s for s in spans if _looks_like_transaction(s.text)]
            non_tx_spans = [s for s in spans if not _looks_like_transaction(s.text)]
            if not tx_spans or not non_tx_spans:
                continue

            non_tx_fonts = {s.font for s in non_tx_spans}
            # Modal family — picks the most common non-tx font family.
            modal_family = _modal_family(non_tx_spans)
            if not modal_seen and modal_family:
                modal_seen = modal_family

            # Body size — only when non-tx has a single shared size. With
            # heading+body mix this is None and the size check is skipped.
            body_size: float | None = _body_size_if_uniform(non_tx_spans)

            # Family mismatch (strongest signal).
            tx_fonts_not_in_non_tx = {s.font for s in tx_spans if s.font not in non_tx_fonts}

            # Size delta — only meaningful when body_size is well-defined.
            size_delta_breached = False
            if body_size is not None:
                size_delta_breached = any(
                    abs(s.size - body_size) > _SIZE_DELTA_THRESHOLD for s in tx_spans
                )

            # Differing-profile fraction — bound to this page's modal
            # and body_size via closure variables passed positionally so
            # ruff's B023 (loop-var binding in def) doesn't fire.
            differing_count = _count_differing(
                tx_spans,
                modal_family=modal_family,
                body_size=body_size,
            )
            differing_pct = differing_count / len(tx_spans)

            page_flagged = (
                bool(tx_fonts_not_in_non_tx)
                or size_delta_breached
                or differing_pct > _DIFFERING_PCT_THRESHOLD
            )
            if page_flagged:
                affected_pages += 1
                anomalous_fonts.update(tx_fonts_not_in_non_tx)

        if affected_pages == 0:
            return _NULL_RESULT

        confidence = min(1.0, affected_pages / max(1, total_pages))
        return FontConsistencyResult(
            inconsistency_detected=True,
            affected_page_count=affected_pages,
            modal_font=modal_seen,
            anomalous_fonts=sorted(anomalous_fonts),
            confidence=confidence,
        )
    finally:
        doc.close()


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Span:
    """One text span extracted from a page — the unit of comparison."""

    text: str
    font: str
    size: float


def _collect_spans(text_dict: dict[str, object]) -> list[_Span]:
    """Walk pymupdf's ``page.get_text("dict")`` structure into flat spans.

    Skips spans with empty / whitespace-only text (won't carry useful
    font signal), and skips non-text blocks (images, shadings —
    ``block["type"] != 0``).
    """
    out: list[_Span] = []
    blocks = text_dict.get("blocks", [])
    if not isinstance(blocks, list):
        return out
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        if not isinstance(lines, list):
            continue
        for line in lines:
            if not isinstance(line, dict):
                continue
            spans = line.get("spans", [])
            if not isinstance(spans, list):
                continue
            for span in spans:
                if not isinstance(span, dict):
                    continue
                text = span.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    continue
                font = span.get("font", "")
                size = span.get("size", 0.0)
                if not isinstance(font, str):
                    continue
                if not isinstance(size, (int, float)):
                    continue
                out.append(_Span(text=text, font=font, size=float(size)))
    return out


def _modal_family(spans: list[_Span]) -> str:
    """Most common font family across the spans (ignoring size).

    Picking by FAMILY rather than ``(family, size)`` is the operative
    move: bank statements routinely use one family at two sizes
    (headings + body), so ``(family, size)`` modals can spuriously land
    on a heading entry. Family alone is the page's "voice"; size is
    handled separately in ``_body_size_if_uniform``.

    Ties broken by first-seen order (Python dicts preserve insertion).
    Returns ``""`` on empty input (caller gates on this; defensive).
    """
    if not spans:
        return ""
    counts: dict[str, int] = {}
    for s in spans:
        counts[s.font] = counts.get(s.font, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0]


def _count_differing(
    tx_spans: list[_Span],
    *,
    modal_family: str,
    body_size: float | None,
) -> int:
    """Count transaction spans whose profile differs from page modal.

    "Differs" = different family OR (body_size is well-defined AND
    size delta exceeds the threshold). Pulled out of ``analyze`` as a
    module-level helper because ruff's B023 (loop-variable binding in
    def) flags inner-def closures that reference per-iteration vars.
    """
    differing = 0
    for s in tx_spans:
        if s.font != modal_family:
            differing += 1
            continue
        if body_size is not None and abs(s.size - body_size) > _SIZE_DELTA_THRESHOLD:
            differing += 1
    return differing


def _body_size_if_uniform(spans: list[_Span]) -> float | None:
    """Return the single shared size of ``spans`` when there's one, else
    None.

    "Uniform" means: all non-transaction spans round to the same size
    within 0.5pt. When non-tx spans mix sizes (headings 14pt + body
    10pt), the size signal is too noisy to compare transaction-span
    sizes against, so the caller skips the size check entirely for
    that page. The family check remains in force regardless.
    """
    if not spans:
        return None
    sizes = sorted({round(s.size, 1) for s in spans})
    if len(sizes) == 1:
        return sizes[0]
    # Allow a tight rounding band: all sizes within 0.5pt of each other
    # count as "uniform" (handles font hinting variance on the same
    # nominal size).
    if max(sizes) - min(sizes) <= 0.5:
        return sum(sizes) / len(sizes)
    return None


__all__ = ["FontConsistencyResult", "analyze"]
