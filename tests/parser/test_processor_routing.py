"""Processor routing decision tests.

``aegis.parser.processor.processor_type_for_document`` is the public
helper the upload route + the bank pipeline use to decide whether
a document belongs to the processor pipeline or the bank pipeline.

Three cases pinned by these tests:
  1. Filename match → ``"stripe"`` (no PDF required).
  2. Content match (PDF signatures) → ``"stripe"`` (no filename match).
  3. No filename match + no signatures → ``None`` (bank fallback).
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from aegis.parser.processor import (
    detect_processor_from_filename,
    processor_type_for_document,
)


def test_filename_routes_to_stripe() -> None:
    """A Dashboard balance-transactions filename triggers Stripe even
    if no PDF is provided. CSV uploads don't have content the
    PDF-content detector can see, so the filename hit is load-bearing."""
    assert (
        processor_type_for_document(
            filename="balance_transactions_2026-03.csv",
            pdf_path=None,
        )
        == "stripe"
    )


def test_filename_stripe_payouts_routes_to_stripe() -> None:
    """``stripe_payouts_<quarter>.pdf`` is the other common Stripe
    export shape."""
    assert (
        processor_type_for_document(
            filename="stripe_payouts_Q1.pdf",
            pdf_path=None,
        )
        == "stripe"
    )


def test_content_match_routes_to_stripe(tmp_path: Path) -> None:
    """No filename hit, but the PDF content carries Stripe signatures
    (brand + activity summary heading). Falls through to the content
    detector which flips to Stripe."""
    pdf = tmp_path / "merchant_statement_q1.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 72),
        "\n".join(
            [
                "stripe.com",
                "Stripe, Inc.",
                "Activity summary",
                "Gross volume",
                "Net volume",
            ]
        ),
        fontsize=10,
    )
    doc.save(pdf)
    doc.close()

    # Filename is silent — gives the content detector room to vote.
    assert detect_processor_from_filename(pdf.name) == "bank"
    assert processor_type_for_document(filename=pdf.name, pdf_path=pdf) == "stripe"


def test_neither_filename_nor_content_routes_to_bank(tmp_path: Path) -> None:
    """A plain bank-statement PDF with no processor signatures returns
    None — the caller routes to the bank pipeline."""
    pdf = tmp_path / "chase_business_checking_march.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 72),
        "JPMorgan Chase Bank N.A.\nBusiness Checking Statement\n",
        fontsize=10,
    )
    doc.save(pdf)
    doc.close()

    assert processor_type_for_document(filename=pdf.name, pdf_path=pdf) is None


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("stripe_balance_transactions.csv", "stripe"),
        ("BALANCE_TRANSACTION_export.csv", "stripe"),
        ("balance-transactions-2026.csv", "stripe"),
        ("payouts_2026_q1.csv", "stripe"),
        ("invoice.pdf", "bank"),
        ("chase_statement.pdf", "bank"),
        # Square tokens
        ("square_sales_summary.pdf", "square"),
    ],
)
def test_filename_detector_matrix(filename: str, expected: str) -> None:
    """Cover the common filename shapes that the operator runs into."""
    assert detect_processor_from_filename(filename) == expected
