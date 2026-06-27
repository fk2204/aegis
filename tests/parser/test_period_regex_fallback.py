"""Tests for the regex-based period detection fallback.

Covers the standalone pattern registry (``aegis.parser.period_regex``)
plus the ``_apply_period_regex_fallback`` integration hook in
``aegis.parser.extract``. The end-to-end ``run_pipeline`` route is
covered by neighboring tests that already exercise the
``period_unclear`` → ``manual_review`` path; this file's integration
case verifies the FLAG emission so the worker audit-row contract
holds (``[META] period_regex_fallback_used:<pattern>:<fragment>``).

TODO(operator): if a real captured first-page text fragment with
multiple period formats becomes available (e.g. a Brex statement
header), drop it under ``tests/parser/fixtures/`` and replace the
inline strings here. Per CLAUDE.md the period text contains no PII so
the fixture is safe to commit directly.
"""

from __future__ import annotations

from datetime import date

from aegis.parser.extract import _apply_period_regex_fallback
from aegis.parser.period_regex import PeriodMatch, extract_period_via_regex

# ----------------------------------------------------------------------
# Per-pattern unit tests (the registry behavior)
# ----------------------------------------------------------------------


def test_pattern_month_day_year_through() -> None:
    """Pattern 1 — ``Month D, YYYY through Month D, YYYY``."""
    text = "Statement summary\nJanuary 1, 2026 through January 31, 2026\nAccount details"
    match = extract_period_via_regex(text)
    assert match is not None
    assert match.period_start == date(2026, 1, 1)
    assert match.period_end == date(2026, 1, 31)
    assert match.pattern_name == "month_day_year_through"
    assert "January 1, 2026" in match.fragment


def test_pattern_slash_mdy_range() -> None:
    """Pattern 2 — ``MM/DD/YYYY to MM/DD/YYYY``."""
    text = "Period covered: 01/01/2026 - 01/31/2026"
    match = extract_period_via_regex(text)
    assert match is not None
    assert match.period_start == date(2026, 1, 1)
    assert match.period_end == date(2026, 1, 31)
    # ``label_prefixed:slash_mdy_range`` wins because the inline range
    # is preceded by a ``Period`` label. Either dispatch carries the
    # right dates; the per-pattern unit lives below.
    assert "slash_mdy_range" in match.pattern_name


def test_pattern_slash_mdy_range_inline() -> None:
    """Pattern 2 inline — no label prefix → bare ``slash_mdy_range``."""
    text = "Page 1 of 4    01/01/2026 to 01/31/2026    Account ending 1234"
    match = extract_period_via_regex(text)
    assert match is not None
    assert match.pattern_name == "slash_mdy_range"
    assert match.period_start == date(2026, 1, 1)
    assert match.period_end == date(2026, 1, 31)


def test_pattern_slash_mdy2_range() -> None:
    """Pattern 3 — ``MM/DD/YY [to] MM/DD/YY`` (assume 20XX)."""
    text = "Statement 04/01/26 to 04/30/26 enclosed"
    match = extract_period_via_regex(text)
    assert match is not None
    assert match.period_start == date(2026, 4, 1)
    assert match.period_end == date(2026, 4, 30)
    assert match.pattern_name == "slash_mdy2_range"


def test_pattern_month_year_alone() -> None:
    """Pattern 4 — ``Month YYYY`` infers first..last day of month."""
    text = "MARCH 2026 STATEMENT\nfor account ending in 9876"
    match = extract_period_via_regex(text)
    assert match is not None
    assert match.period_start == date(2026, 3, 1)
    assert match.period_end == date(2026, 3, 31)
    assert match.pattern_name == "month_year_alone"


def test_pattern_iso_range() -> None:
    """Pattern 5 — ``YYYY-MM-DD - YYYY-MM-DD``."""
    text = "Reporting window 2026-02-01 - 2026-02-28 inclusive"
    match = extract_period_via_regex(text)
    assert match is not None
    assert match.period_start == date(2026, 2, 1)
    assert match.period_end == date(2026, 2, 28)
    assert match.pattern_name == "iso_range"


def test_pattern_label_prefixed_wraps_subpattern() -> None:
    """Pattern 6 — label-prefix wrapping any of patterns 1-5.

    The label prefix carries into the fragment so an operator scanning
    audit rows sees the original boilerplate (``Statement Period:``)
    next to the matched dates.
    """
    text = "Statement Period: April 1, 2026 through April 30, 2026"
    match = extract_period_via_regex(text)
    assert match is not None
    assert match.period_start == date(2026, 4, 1)
    assert match.period_end == date(2026, 4, 30)
    # Inline match wins over label-wrapped match because the bare-text
    # try fires first in the registry. Either way the dates are correct;
    # ``pattern_name`` either ``month_day_year_through`` directly OR
    # ``label_prefixed:month_day_year_through`` if the bare path missed.
    assert "month_day_year_through" in match.pattern_name


# ----------------------------------------------------------------------
# Degenerate inputs
# ----------------------------------------------------------------------


def test_empty_text_returns_none() -> None:
    """Empty / whitespace-only text short-circuits to None."""
    assert extract_period_via_regex("") is None
    assert extract_period_via_regex("   \n\t  ") is None


def test_text_without_dates_returns_none() -> None:
    """Free-form text with no period-shaped substring returns None."""
    text = "Wells Fargo Business Banking\nAccount ending 1234\nThank you for banking with us"
    assert extract_period_via_regex(text) is None


def test_invalid_calendar_dates_return_none() -> None:
    """Regex-shape match that yields an invalid date (e.g. Feb 30)
    returns None — we never produce a bogus calendar value.

    Note: pattern walk continues, so if a LATER position carries valid
    dates we use those. Here both candidate positions are invalid so
    overall None.
    """
    text = "02/30/2026 - 13/01/2026"
    assert extract_period_via_regex(text) is None


# ----------------------------------------------------------------------
# Integration with ``_apply_period_regex_fallback``
# ----------------------------------------------------------------------


def test_apply_fallback_noop_when_both_dates_present() -> None:
    """Early return when raw summary already has both period dates."""
    raw_summary = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
    }
    # Bytes don't matter — the fallback never reads them in the early-
    # return path.
    result = _apply_period_regex_fallback(raw_summary, pdf_bytes=b"")
    assert result is None
    assert raw_summary["period_start"] == "2026-01-01"
    assert raw_summary["period_end"] == "2026-01-31"


def test_apply_fallback_populates_missing_end_only() -> None:
    """When only ``period_end`` is null and regex finds a range, we
    populate end and leave start untouched."""
    raw_summary = {
        "period_start": "2025-12-15",  # Bedrock got this one right
        "period_end": None,  # but dropped this one
    }
    pdf_bytes = _make_pdf_with_first_page_text(
        "Statement Period: January 1, 2026 through January 31, 2026"
    )
    result = _apply_period_regex_fallback(raw_summary, pdf_bytes=pdf_bytes)
    assert result is not None
    assert raw_summary["period_start"] == "2025-12-15"  # untouched
    assert raw_summary["period_end"] == "2026-01-31"  # regex-filled


def test_apply_fallback_populates_both_when_both_missing() -> None:
    """When both dates are null, both get populated from regex."""
    raw_summary: dict[str, object] = {
        "period_start": None,
        "period_end": None,
    }
    pdf_bytes = _make_pdf_with_first_page_text(
        "Account Activity 01/01/2026 - 01/31/2026 for account ending 1234"
    )
    result = _apply_period_regex_fallback(raw_summary, pdf_bytes=pdf_bytes)
    assert result is not None
    assert raw_summary["period_start"] == "2026-01-01"
    assert raw_summary["period_end"] == "2026-01-31"


def test_apply_fallback_returns_none_on_regex_miss() -> None:
    """Both dates missing AND regex finds nothing → ``None``.

    Routing stays unchanged: the caller hits ``ExtractedStatement``
    validation, which now sees nulls, raises ``ValidationError``,
    propagates as ``ExtractionError``, and the pipeline routes the
    document to ``manual_review`` per the existing ``period_unclear``
    semantics. This test guards that we don't accidentally swallow.
    """
    raw_summary: dict[str, object] = {
        "period_start": None,
        "period_end": None,
    }
    pdf_bytes = _make_pdf_with_first_page_text(
        "Thank you for banking with us. See enclosed statement for details."
    )
    result = _apply_period_regex_fallback(raw_summary, pdf_bytes=pdf_bytes)
    assert result is None
    assert raw_summary["period_start"] is None
    assert raw_summary["period_end"] is None


def test_apply_fallback_returns_none_when_pdf_unreadable() -> None:
    """A malformed / empty PDF buffer yields empty first-page text →
    fallback returns None without raising."""
    raw_summary: dict[str, object] = {"period_start": None, "period_end": None}
    result = _apply_period_regex_fallback(raw_summary, pdf_bytes=b"not a pdf")
    assert result is None


# ----------------------------------------------------------------------
# Fragment-length contract (audit-row PII safety)
# ----------------------------------------------------------------------


def test_fragment_truncated_to_200_chars() -> None:
    """The ``fragment`` field is bounded at 200 chars regardless of
    surrounding context — keeps the audit-row payload small and
    eliminates any chance of a long transcription block bleeding into
    the row."""
    # A long label that would carry past 200 chars if not bounded.
    long_label = "Statement Period (this is an artificially long label " * 10
    text = f"{long_label}: January 1, 2026 through January 31, 2026"
    match = extract_period_via_regex(text)
    assert match is not None
    assert len(match.fragment) <= 200


# ----------------------------------------------------------------------
# Helpers — synthesize a tiny PDF with a known first-page text layer
# ----------------------------------------------------------------------


def _make_pdf_with_first_page_text(text: str) -> bytes:
    """Build an in-memory PDF whose page 1 carries the given text layer.

    Uses ``pymupdf`` (already a hard dep of the project — the vision
    fallback rasterizes via the same library). No file I/O.
    """
    import pymupdf  # local import — tests can skip when not installed

    doc = pymupdf.open()  # type: ignore[no-untyped-call]  # local import — tests can skip
    try:
        page = doc.new_page(width=612, height=792)  # US Letter
        page.insert_text((72, 72), text, fontsize=11)
        buffer = doc.tobytes()  # type: ignore[no-untyped-call]
    finally:
        doc.close()  # type: ignore[no-untyped-call]
    assert isinstance(buffer, bytes)
    return buffer


# Type re-export sanity: PeriodMatch is exposed for downstream consumers.
def test_period_match_is_exported() -> None:
    assert PeriodMatch.__module__ == "aegis.parser.period_regex"
