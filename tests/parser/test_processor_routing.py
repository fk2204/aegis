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
    detect_processor_from_csv_header,
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
        # Square tokens — branded
        ("square_sales_summary.pdf", "square"),
        # Square Dashboard's default ``transactions_<date>.csv`` filename
        # is NOT routable by filename alone — it collides with Stripe's
        # ``balance_transactions_<date>.csv`` substring. Routes to bank
        # at the filename layer; the CSV header sniff in
        # detect_processor_from_csv_header is the correct discriminator.
        ("transactions_2026-03-01_2026-03-31.csv", "bank"),
        # Square tokens — alternate dash form some operators rename to
        ("square-transactions-march.csv", "square"),
    ],
)
def test_filename_detector_matrix(filename: str, expected: str) -> None:
    """Cover the common filename shapes that the operator runs into."""
    assert detect_processor_from_filename(filename) == expected


# ---------------------------------------------------------------------------
# CSV header-content routing — the renamed-to-export.csv case
# ---------------------------------------------------------------------------


def test_csv_header_detects_square_signature() -> None:
    """A CSV whose filename was renamed to a generic ``export.csv``
    still routes to Square when its header carries the canonical
    Square signature."""
    header = (
        "Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID,"
        "Payment ID,Card Brand,PAN Suffix,Device Name,Notes,Event Type,Location"
    )
    assert detect_processor_from_csv_header(header) == "square"


def test_csv_header_detects_stripe_signature() -> None:
    """Stripe balance-transactions header detection by the first three
    columns."""
    header = "id,Type,Source,Amount,Fee,Net,Currency,Created (UTC)"
    assert detect_processor_from_csv_header(header) == "stripe"


def test_csv_header_no_signature_returns_bank() -> None:
    """A header that carries neither signature falls through to the
    bank fallback (the caller refuses an untagged CSV)."""
    header = "Transaction Date,Posting Date,Description,Amount,Category"
    assert detect_processor_from_csv_header(header) == "bank"


def test_csv_header_empty_returns_bank() -> None:
    assert detect_processor_from_csv_header("") == "bank"


def test_csv_header_strips_utf8_bom() -> None:
    """Square + Stripe Dashboard exports ship the UTF-8 BOM. The
    detector must tolerate it on the leading character so a raw
    decoded line still matches."""
    header = "﻿Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID,extra"
    assert detect_processor_from_csv_header(header) == "square"
