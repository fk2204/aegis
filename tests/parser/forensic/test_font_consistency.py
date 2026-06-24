"""Tests for ``aegis.parser.forensic.font_consistency``.

Each test builds a synthetic PDF inline with reportlab (already in the
stack for the corpus generator), then runs ``analyze`` on it. The
synthetic-PDF approach keeps the test corpus self-contained and
deterministic — no fixture files to commit, no operator-provided PDFs
mixed into the unit-test path.

Coverage:

* Clean PDF (uniform font across non-transaction text + transaction
  rows) → ``inconsistency_detected=False``.
* Synthetic PDF with TWO font families on transaction rows where
  non-transaction text is one family → ``inconsistency_detected=True``,
  affected_page_count == 1, anomalous_fonts carries the alien family.
* Single-font, single-page PDF (no inter-row variance possible) →
  ``inconsistency_detected=False`` (no false positive).
* PDF with no extractable text → graceful fallback,
  ``inconsistency_detected=False``, no crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

from aegis.parser.forensic.font_consistency import (
    FontConsistencyResult,
    analyze,
)

# ---------------------------------------------------------------------
# Helpers — build synthetic PDFs with controlled fonts per text run
# ---------------------------------------------------------------------


def _make_clean_pdf(path: Path) -> None:
    """Single-page PDF, one font throughout. Transaction rows and
    non-transaction text both use Helvetica at 10pt."""
    c = rl_canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 14)
    c.drawString(72, 720, "Bank Statement — Acme Coffee LLC")
    c.drawString(72, 700, "Statement Period: 04/01/2026 to 04/30/2026")
    c.setFont("Helvetica", 10)
    c.drawString(72, 670, "Account Summary")
    c.drawString(72, 650, "Beginning Balance: $5,000.00")
    c.drawString(72, 630, "Ending Balance: $4,800.00")
    # Transaction rows — Helvetica too, same as headings.
    c.drawString(72, 600, "04/02   ATM Withdrawal               -100.00")
    c.drawString(72, 580, "04/05   Card Purchase Coffee Co       -8.50")
    c.drawString(72, 560, "04/12   Direct Deposit Payroll       1,200.00")
    c.drawString(72, 540, "04/20   ACH Debit Utility            -156.00")
    c.save()


def _make_pasted_over_pdf(path: Path) -> None:
    """Single-page PDF where non-transaction text is Helvetica but the
    transaction rows are Courier (a clearly different family). Mirrors
    paste-over fraud — surrounding context rendered in tool A, rows
    pasted in from tool B."""
    c = rl_canvas.Canvas(str(path), pagesize=letter)
    # Heading + summary block in Helvetica (the "page voice").
    c.setFont("Helvetica", 14)
    c.drawString(72, 720, "Bank Statement — Victim Industries LLC")
    c.drawString(72, 700, "Statement Period: 04/01/2026 to 04/30/2026")
    c.setFont("Helvetica", 10)
    c.drawString(72, 670, "Account Summary")
    c.drawString(72, 650, "Beginning Balance: $1,000.00")
    c.drawString(72, 630, "Ending Balance: $50,000.00")
    c.drawString(72, 610, "Total Deposits: 49,200.00")
    # Transaction rows in Courier — different family, currency tokens
    # present so the row-classifier picks them up.
    c.setFont("Courier", 10)
    c.drawString(72, 580, "04/02   Wire In Phantom Inc            25,000.00")
    c.drawString(72, 560, "04/05   Wire In Phantom Inc            24,200.00")
    c.drawString(72, 540, "04/12   Card Purchase Sandwich         -8.50")
    c.drawString(72, 520, "04/20   ACH Debit Utility              -156.00")
    c.save()


def _make_single_font_one_page_pdf(path: Path) -> None:
    """Edge case: very short single-page PDF, one font, no real
    transaction-vs-summary contrast — just two lines. Verify the
    detector doesn't false-positive on degenerate input."""
    c = rl_canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 11)
    c.drawString(72, 720, "Just one line.")
    c.save()


def _make_empty_image_only_pdf(path: Path) -> None:
    """Edge case: PDF with no text at all (mimics image-only / scanned
    statements). reportlab still produces a valid PDF skeleton; the
    detector should fall back to no-flag rather than crash."""
    c = rl_canvas.Canvas(str(path), pagesize=letter)
    # No drawString calls — single empty page.
    c.showPage()
    c.save()


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_clean_pdf_uniform_fonts_no_inconsistency(tmp_path: Path) -> None:
    """Single-page PDF with one font throughout → no inconsistency."""
    pdf_path = tmp_path / "clean.pdf"
    _make_clean_pdf(pdf_path)

    result = analyze(pdf_path)

    assert isinstance(result, FontConsistencyResult)
    assert result.inconsistency_detected is False
    assert result.affected_page_count == 0
    assert result.anomalous_fonts == []
    assert result.confidence == 0.0


def test_pasted_over_transaction_rows_trigger_inconsistency(tmp_path: Path) -> None:
    """Transaction rows in Courier vs Helvetica surroundings → page
    flagged inconsistent; the alien family lands in ``anomalous_fonts``."""
    pdf_path = tmp_path / "pasted.pdf"
    _make_pasted_over_pdf(pdf_path)

    result = analyze(pdf_path)

    assert result.inconsistency_detected is True
    assert result.affected_page_count == 1
    # reportlab embeds Courier as "Courier" — the detector reads
    # whatever font name pymupdf surfaces, which should reflect the
    # embedded name. Just assert that SOMETHING anomalous is captured;
    # the exact string depends on pymupdf's font-name normalisation.
    assert result.anomalous_fonts, (
        f"expected at least one anomalous font; got {result.anomalous_fonts}"
    )
    # Modal font from the non-tx Helvetica spans should be reported.
    assert "Helv" in result.modal_font or result.modal_font.startswith("Helv"), (
        f"expected modal to reflect Helvetica family; got {result.modal_font!r}"
    )
    # Confidence on a 1-of-1 page is 1.0.
    assert result.confidence == pytest.approx(1.0)


def test_single_font_short_pdf_no_false_positive(tmp_path: Path) -> None:
    """One line of text → no transaction-like content → skip page,
    no flag. Verifies the degenerate-input gate."""
    pdf_path = tmp_path / "short.pdf"
    _make_single_font_one_page_pdf(pdf_path)

    result = analyze(pdf_path)

    assert result.inconsistency_detected is False
    assert result.affected_page_count == 0


def test_empty_pdf_graceful_fallback(tmp_path: Path) -> None:
    """PDF with no extractable text → no-flag, no crash. Mirrors the
    image-only / scanned-statement code path."""
    pdf_path = tmp_path / "empty.pdf"
    _make_empty_image_only_pdf(pdf_path)

    result = analyze(pdf_path)

    assert result.inconsistency_detected is False
    assert result.affected_page_count == 0


def test_nonexistent_path_returns_null_result(tmp_path: Path) -> None:
    """A path that doesn't exist → ``fitz.open`` raises, caught, returns
    null result. Confirms the catch-all in the public ``analyze``."""
    result = analyze(tmp_path / "definitely_missing.pdf")

    assert result.inconsistency_detected is False
    assert result.affected_page_count == 0
    assert result.modal_font == ""
