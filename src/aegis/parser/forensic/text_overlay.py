"""Text-overlay detector — paste-over fraud signature.

When an attacker overwrites transactions on a bank PDF, the cheapest
attack is to leave the original bank-issued content stream intact and
add a SECOND content stream that renders replacement text at the same
on-page positions. The replacement text visually covers the originals
in the rendered PDF but both streams remain in the file. This detector
finds that signature.

Legitimate multi-stream PDFs are common — some bank exports split
chrome / body / footer into separate streams for download speed. The
discriminator between legitimate and paste-over: in legitimate multi-
stream PDFs the streams render in NON-overlapping page regions (header
at top, body in middle, footer at bottom); paste-over fraud needs the
attack stream to overlap the original at the SAME positions to
visually replace the underlying text.

Algorithm
---------
Per page:

1. Read the page's /Contents object via pikepdf. Skip if it's not an
   array (single stream → no overlay possible).
2. Parse each stream individually with ``pikepdf.parse_content_stream``.
   Walk operators inside BT/ET text blocks, track the text matrix's
   Y coordinate via ``Tm`` / ``Td`` / ``TD``, and record the Y at every
   text-render operator (``Tj``, ``TJ``, ``'``, ``"``).
3. Collapse each stream's Y-positions into a ``(min_y, max_y)`` range.
   Skip streams with no text — they contribute graphics / images only.
4. Compare every pair of text-bearing streams. If ANY pair has
   overlapping Y-ranges (intersection non-empty), the page is flagged.

The Y-only check is intentionally simpler than full 2D bounding-box
intersection. Paste-over attacks on bank-statement transaction rows
always overlap on the Y axis (the attack reproduces the same rows on
the same lines), so Y-overlap is sufficient discrimination. False
positives on legitimate exports that share a Y-range across streams
(rare; most layouts split by Y) are an acceptable cost — the operator
sees the flag and triages.

Failure modes deliberately fall back to no-flag:
  * pikepdf can't open / parse the PDF → null result.
  * Per-page stream parsing fails → skip that page, continue.
  * Page's /Contents is missing / null / scalar → no overlay possible
    by definition (single stream is the baseline).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pikepdf

# ─────────────────────────────────────────────────────────────────────
# Result shape
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TextOverlayResult:
    """Document-level rollup of per-page text-overlay findings.

    * ``overlay_detected`` is the gate the caller in ``metadata.py``
      reads to decide whether to surface ``[META] text_overlay_detected``.
    * ``affected_pages`` is the 1-based list of page numbers flagged.
      Operator-facing — easier to navigate to "page 4" than to think
      in 0-based indexes.
    * ``overlay_stream_count`` is the total number of text-bearing
      streams across all affected pages. Diagnostic only — not used
      in scoring.
    """

    overlay_detected: bool
    affected_pages: list[int] = field(default_factory=list)
    overlay_stream_count: int = 0


_NULL_RESULT: Final[TextOverlayResult] = TextOverlayResult(
    overlay_detected=False,
    affected_pages=[],
    overlay_stream_count=0,
)


# Operators that render text. Recording the text-matrix Y at any of
# these is what surfaces "this stream draws text at Y=...".
_TEXT_SHOW_OPERATORS: Final[frozenset[str]] = frozenset({"Tj", "TJ", "'", '"'})

# Operators that update the text matrix (text-space coordinates).
_TEXT_MATRIX_SET_OPERATOR: Final[str] = "Tm"  # absolute, takes 6 numbers
_TEXT_MOVE_OPERATORS: Final[frozenset[str]] = frozenset({"Td", "TD"})  # relative


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def analyze(pdf_path: str | Path) -> TextOverlayResult:
    """Walk every page; flag those whose multiple text-bearing streams
    have overlapping Y-ranges.

    Conservative fallback: any open / parse failure returns the null
    result.
    """
    path = Path(pdf_path)
    try:
        pdf = pikepdf.open(str(path))
    except Exception:
        return _NULL_RESULT

    try:
        affected_pages: list[int] = []
        overlay_stream_count = 0

        for page_index, page in enumerate(pdf.pages, start=1):
            stream_ranges = _per_stream_y_ranges(page)
            if len(stream_ranges) < 2:
                continue
            if _any_pair_overlaps(stream_ranges):
                affected_pages.append(page_index)
                overlay_stream_count += len(stream_ranges)

        if not affected_pages:
            return _NULL_RESULT

        return TextOverlayResult(
            overlay_detected=True,
            affected_pages=affected_pages,
            overlay_stream_count=overlay_stream_count,
        )
    finally:
        pdf.close()


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _per_stream_y_ranges(page: pikepdf.Page) -> list[tuple[float, float]]:
    """Return ``(min_y, max_y)`` per text-bearing content stream on the
    page.

    Streams with no text-render operators contribute nothing and are
    excluded. A page with a single stream returns a single-entry list
    (the caller's >=2 gate filters it out).
    """
    try:
        contents: Any = page.get("/Contents")
    except (pikepdf.PdfError, KeyError, ValueError):
        return []
    if contents is None:
        return []
    # Single-stream pages: /Contents is a Stream object, not an Array.
    # No overlay possible by definition.
    if not isinstance(contents, pikepdf.Array):
        return []

    ranges: list[tuple[float, float]] = []
    # pikepdf's Array stubs don't expose the iteration protocol that
    # mypy expects on ``list(contents)``; iterate via integer index
    # using ``len`` + ``__getitem__`` which IS in the stubs.
    for i in range(len(contents)):
        stream = contents[i]
        y_positions = _extract_text_y_positions(stream)
        if not y_positions:
            continue
        ranges.append((min(y_positions), max(y_positions)))
    return ranges


def _extract_text_y_positions(stream: Any) -> list[float]:  # noqa: ANN401 — pikepdf stream object, weakly typed in stubs
    """Parse a single content stream and return the Y-coordinate at
    every text-render operator inside BT/ET blocks.

    The text matrix Y is tracked via ``Tm`` (absolute, element 5 = ty)
    and ``Td`` / ``TD`` (relative, second operand = ty). The CTM is
    intentionally ignored — bank PDFs typically don't transform text
    blocks via cm, and the comparison we make is *between* streams on
    the same page (both subject to the same CTM if any), so omitting
    CTM doesn't bias the overlap detection.
    """
    try:
        operations = pikepdf.parse_content_stream(stream)
    except (pikepdf.PdfError, TypeError, ValueError):
        return []

    positions: list[float] = []
    in_text = False
    # Text matrix Y. Reset at BT per the PDF spec — the text matrix is
    # established to identity at the start of every BT block.
    text_y = 0.0

    # pikepdf's parse_content_stream yields ContentStreamInstruction (or
    # ContentStreamInlineImage) instances with ``.operands`` and
    # ``.operator`` attributes — NOT 2-tuples. Older API examples that
    # tuple-unpack rely on dunder support that mypy doesn't see.
    for instruction in operations:
        op_attr = getattr(instruction, "operator", None)
        operands_attr = getattr(instruction, "operands", None)
        if op_attr is None or operands_attr is None:
            # Inline-image instruction or other non-text op — skip.
            continue
        op_name = _operator_name(op_attr)
        operands = list(operands_attr)

        if op_name == "BT":
            in_text = True
            text_y = 0.0
            continue
        if op_name == "ET":
            in_text = False
            continue
        if not in_text:
            continue

        if op_name == _TEXT_MATRIX_SET_OPERATOR and len(operands) >= 6:
            # Tm: [a b c d e f] — f is ty.
            ty = _to_float(operands[5])
            if ty is not None:
                text_y = ty
            continue
        if op_name in _TEXT_MOVE_OPERATORS and len(operands) >= 2:
            # Td / TD: [tx ty] — relative move.
            dy = _to_float(operands[1])
            if dy is not None:
                text_y += dy
            continue
        if op_name in _TEXT_SHOW_OPERATORS:
            positions.append(text_y)

    return positions


def _operator_name(operator: Any) -> str:  # noqa: ANN401 — pikepdf Operator, may also be bytes in older versions
    """Return a stable string name for a pikepdf operator.

    pikepdf's parse_content_stream yields ``pikepdf.Operator``
    instances; ``str()`` on an Operator returns the PDF operator name
    (``"BT"``, ``"Tj"``, etc.). Defensive against the rare case where
    a future pikepdf release returns bytes.
    """
    if isinstance(operator, bytes):
        return operator.decode("ascii", errors="ignore")
    return str(operator)


def _to_float(value: Any) -> float | None:  # noqa: ANN401 — pikepdf numeric, weakly typed in stubs
    """Coerce a pikepdf numeric operand to float, returning None on
    failure. pikepdf wraps numerics in ``pikepdf.Object`` subclasses
    that support ``float(x)`` cleanly in current versions; the helper
    exists to make the failure path explicit + greppable."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _any_pair_overlaps(ranges: list[tuple[float, float]]) -> bool:
    """True when any pair of (min_y, max_y) ranges has a non-empty
    intersection."""
    for i, (a_lo, a_hi) in enumerate(ranges):
        for b_lo, b_hi in ranges[i + 1 :]:
            # Intersection is non-empty when ``max(lows) <= min(highs)``.
            if max(a_lo, b_lo) <= min(a_hi, b_hi):
                return True
    return False


__all__ = ["TextOverlayResult", "analyze"]
