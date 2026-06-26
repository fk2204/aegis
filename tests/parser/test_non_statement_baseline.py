"""Empirical baseline — what does the parser do with NON-statement PDFs?

The 2026-05-27 access-model audit surfaced that Feature 2's chunk-3
orchestrator would auto-pull every PDF on a Close Lead through the
parser. The corpus tests guarantee the parser HANDLES real statements
correctly — but they say nothing about behavior on the wrong document
type (driver's license, voided check, application form, vendor
invoice). This module fills that gap.

Four failure modes possible for a non-statement PDF:

  A — LLM refuses or returns malformed JSON → ExtractionError raised
      → upstream worker writes ``document.parse.error``. Clean reject.

  B — LLM returns parseable JSON but Pydantic validation fails (e.g.
      missing required summary fields, invalid period dates) →
      ExtractionError raised. Clean reject.

  C-1 — LLM returns an "extraction" with all-zero summary (no
      transactions, beginning=0, ending=0, deposit_total=0,
      withdrawal_total=0) AND a plausible 14-50 day period. Period
      reconciliation: 0+0-0=0 ✓. Listed-vs-printed: 0==0 ✓. Daily
      balance: no transactions to reconcile ✓. **Validation passes.**
      Document lands as ``proceed`` with a zero analysis. The
      operator sees "weak deal" rather than "wrong document." The
      SILENT FAILURE we're testing for.

  C-2 — LLM hallucinates plausible transactions. They don't reconcile
      against fabricated boundary balances OR daily running balance
      breaks. → ``manual_review``. Acceptable.

If any fixture produces case C-1, the deterministic validation gate
in ``aegis.parser.validate`` is extended to fail the
all-zeros-no-transactions shape — a real statement effectively never
has zero deposits AND zero withdrawals AND zero transactions over a
14-50 day window.

Skipped by default. Run with:
    AEGIS_DATA_RESIDENCY_CONFIRMED=true RUN_BEDROCK_TESTS=true \\
        pytest tests/parser/test_non_statement_baseline.py -v -s
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from aegis.parser.extract import ExtractionError, extract_statement
from aegis.parser.validate import validate_extraction

# Gated env var. CI skips; one-shot manual runs set it to true.
_RUN_BEDROCK_TESTS = os.environ.get("RUN_BEDROCK_TESTS", "").lower() == "true"
_SKIP_REASON = (
    "set RUN_BEDROCK_TESTS=true to exercise the parser against real Bedrock "
    "(~$0.05-0.30 per fixture)"
)


# ---------------------------------------------------------------------
# Fixture generators (deterministic via reportlab invariant=True).
# ---------------------------------------------------------------------


def _drivers_license(out_path: Path) -> None:
    """Florida driver's license. No financial structure."""
    c = canvas.Canvas(str(out_path), pagesize=LETTER, invariant=True)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(1 * inch, 10 * inch, "STATE OF FLORIDA")
    c.drawString(1 * inch, 9.6 * inch, "DRIVER LICENSE")
    c.setFont("Helvetica", 11)
    c.drawString(1 * inch, 9.0 * inch, "DL: F600-123-456-789-0")
    c.drawString(1 * inch, 8.6 * inch, "Class: E    Endorsements: NONE")
    c.drawString(1 * inch, 8.2 * inch, "Expires: 04/15/2030")
    c.rect(1 * inch, 6.0 * inch, 1.5 * inch, 1.8 * inch)  # photo placeholder
    c.drawString(2.8 * inch, 7.5 * inch, "JOHN Q SAMPLE")
    c.drawString(2.8 * inch, 7.1 * inch, "1234 MAIN STREET")
    c.drawString(2.8 * inch, 6.7 * inch, "MIAMI, FL 33101")
    c.drawString(2.8 * inch, 6.3 * inch, "DOB: 01/15/1985")
    c.drawString(2.8 * inch, 6.0 * inch, "SEX: M    HAIR: BRN    EYES: BRN")
    c.drawString(2.8 * inch, 5.7 * inch, "HEIGHT: 5'-10\"    WEIGHT: 175 lb")
    c.showPage()
    c.save()


def _voided_check(out_path: Path) -> None:
    """Voided personal check. Has a $ amount line but no balance flow."""
    c = canvas.Canvas(str(out_path), pagesize=LETTER, invariant=True)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 8.5 * inch, "ACME RETAIL LLC")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 8.2 * inch, "555 Commerce Boulevard")
    c.drawString(1 * inch, 8.0 * inch, "Tampa, FL 33602")
    c.drawString(6.5 * inch, 8.5 * inch, "1247")
    c.drawString(6.5 * inch, 8.3 * inch, "Date: __________")
    c.drawString(1 * inch, 7.4 * inch, "PAY TO THE ORDER OF: ________________________________")
    c.drawString(6.5 * inch, 7.4 * inch, "$ ____________")
    c.drawString(1 * inch, 7.0 * inch, "______________________________________________ DOLLARS")
    c.drawString(1 * inch, 6.4 * inch, "BANK OF AMERICA")
    c.drawString(1 * inch, 6.2 * inch, "FOR: __________________")
    c.drawString(1 * inch, 5.6 * inch, "Signature: __________________________")
    # Routing + account numbers
    c.setFont("Courier", 11)
    c.drawString(1 * inch, 5.0 * inch, "⑈063100277⑈ 898012345⑈ 1247")
    # VOID stamp
    c.setFont("Helvetica-Bold", 60)
    c.setFillGray(0.7)
    c.drawString(2.5 * inch, 6.8 * inch, "V O I D")
    c.showPage()
    c.save()


def _vendor_invoice(out_path: Path) -> None:
    """Vendor invoice with $ line items. Closest non-statement to a
    statement structurally — has totals but no balance flow."""
    c = canvas.Canvas(str(out_path), pagesize=LETTER, invariant=True)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(1 * inch, 10.2 * inch, "INVOICE")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.8 * inch, "Invoice #: INV-2026-04-1138")
    c.drawString(1 * inch, 9.6 * inch, "Date: 2026-04-15")
    c.drawString(1 * inch, 9.4 * inch, "Due: 2026-05-15")
    c.drawString(5 * inch, 9.8 * inch, "FROM: Industrial Supply Co.")
    c.drawString(5 * inch, 9.6 * inch, "BILL TO: Acme Retail LLC")
    c.drawString(1 * inch, 8.8 * inch, "DESCRIPTION")
    c.drawString(5 * inch, 8.8 * inch, "QTY")
    c.drawString(6 * inch, 8.8 * inch, "UNIT")
    c.drawString(7 * inch, 8.8 * inch, "AMOUNT")
    lines = [
        ("Cleaning supplies — wholesale lot", 12, 45.00, 540.00),
        ("Display fixture (steel, white)", 4, 175.50, 702.00),
        ("LED bulbs, 60W eq, 8-pack", 30, 18.99, 569.70),
        ("Receipt paper rolls (case of 50)", 6, 32.40, 194.40),
    ]
    y = 8.5
    for desc, qty, unit, amt in lines:
        c.drawString(1 * inch, y * inch, desc)
        c.drawString(5 * inch, y * inch, str(qty))
        c.drawString(6 * inch, y * inch, f"${unit:,.2f}")
        c.drawString(7 * inch, y * inch, f"${amt:,.2f}")
        y -= 0.25
    c.drawString(5 * inch, (y - 0.4) * inch, "Subtotal:")
    c.drawString(7 * inch, (y - 0.4) * inch, "$2,006.10")
    c.drawString(5 * inch, (y - 0.6) * inch, "Tax (7.5%):")
    c.drawString(7 * inch, (y - 0.6) * inch, "$150.46")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(5 * inch, (y - 0.9) * inch, "TOTAL DUE:")
    c.drawString(7 * inch, (y - 0.9) * inch, "$2,156.56")
    c.setFont("Helvetica", 9)
    c.drawString(
        1 * inch, 1 * inch,
        "Payment terms: Net 30. Make checks payable to Industrial Supply Co.",
    )
    c.showPage()
    c.save()


def _application_form(out_path: Path) -> None:
    """Blank merchant cash advance application."""
    c = canvas.Canvas(str(out_path), pagesize=LETTER, invariant=True)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1 * inch, 10.2 * inch, "MERCHANT FUNDING APPLICATION")
    c.setFont("Helvetica", 10)
    fields = [
        ("Legal Business Name", ""),
        ("DBA (if different)", ""),
        ("Federal EIN", ""),
        ("Business Address", ""),
        ("City / State / ZIP", ""),
        ("Owner Full Name", ""),
        ("Owner SSN (last 4)", ""),
        ("Owner Date of Birth", ""),
        ("Business Phone", ""),
        ("Owner Email", ""),
        ("Industry / Business Type", ""),
        ("Date Business Started", ""),
        ("Average Monthly Gross Revenue", ""),
        ("Requested Funding Amount", ""),
        ("Use of Funds", ""),
        ("Existing MCA Position(s)?", ""),
        ("Credit Score Range", ""),
    ]
    y = 9.8
    for label, _ in fields:
        c.drawString(1 * inch, y * inch, f"{label}:")
        c.line(3.5 * inch, y * inch, 7.5 * inch, y * inch)
        y -= 0.32
    c.setFont("Helvetica", 8)
    c.drawString(1 * inch, 1.2 * inch, (
        "By signing below, I authorize Commera Funding to verify the "
        "information provided and to obtain a business credit report."
    ))
    c.drawString(1 * inch, 0.9 * inch, "Signature: _______________________________")
    c.drawString(5 * inch, 0.9 * inch, "Date: ____________")
    c.showPage()
    c.save()


def _off_period_statement(out_path: Path) -> None:
    """Looks like a bank statement but covers only 3 days — invalid_period
    fail expected. Adversarial: the LLM might happily extract this since
    it has all the structural marks of a statement; the deterministic
    validator should catch it."""
    c = canvas.Canvas(str(out_path), pagesize=LETTER, invariant=True)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.4 * inch, "ACME BANK")
    c.drawString(1 * inch, 10.1 * inch, "Statement of Account")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.7 * inch, "ACME RETAIL LLC")
    c.drawString(
        1 * inch, 9.5 * inch,
        "Account: x4321    Statement period: 2026-04-12 to 2026-04-14",
    )
    c.drawString(1 * inch, 9.0 * inch, "Beginning Balance: $12,450.00")
    c.drawString(1 * inch, 8.8 * inch, "Deposits & Credits: $3,200.00")
    c.drawString(1 * inch, 8.6 * inch, "Withdrawals & Debits: $1,890.00")
    c.drawString(1 * inch, 8.4 * inch, "Ending Balance: $13,760.00")
    c.drawString(
        1 * inch, 8.0 * inch,
        "Date       Description                                   Amount",
    )
    rows = [
        ("04-12-2026", "ACH credit STRIPE PAYOUT          ", "+1,200.00"),
        ("04-13-2026", "POS purchase                       ", "  -350.00"),
        ("04-13-2026", "Card batch deposit                 ", "+2,000.00"),
        ("04-14-2026", "Utility ACH debit                  ", "  -890.00"),
        ("04-14-2026", "Wire transfer fee                  ", "   -50.00"),
        ("04-14-2026", "ACH refund                         ", "  -600.00"),
    ]
    y = 7.7
    for d, desc, amt in rows:
        c.drawString(1 * inch, y * inch, f"{d}  {desc}  {amt}")
        y -= 0.22
    c.showPage()
    c.save()


# ---------------------------------------------------------------------
# Test fixtures + result capture
# ---------------------------------------------------------------------


@dataclass
class _FixtureResult:
    fixture: str
    case: str  # "A" | "B" | "C-1" | "C-2" | "control"
    # Values: "extraction_error" | "validation_failed" | "passes_validation"
    parse_status: str | None
    extraction_error: str | None
    validation_failures: list[str]
    extracted_summary: dict[str, Any] | None
    transaction_count: int | None


_FIXTURES = [
    ("drivers_license", _drivers_license),
    ("voided_check", _voided_check),
    ("vendor_invoice", _vendor_invoice),
    ("application_form", _application_form),
    ("off_period_statement", _off_period_statement),
]


@pytest.mark.skipif(not _RUN_BEDROCK_TESTS, reason=_SKIP_REASON)
@pytest.mark.parametrize("fixture_name,generator", _FIXTURES)
def test_non_statement_baseline(
    tmp_path: Path,
    fixture_name: str,
    generator: Any,
) -> None:
    """For each fixture: build PDF, run extract+validate, capture result.

    Hard assert: parse_status MUST NOT be 'passes_validation' (the C-1
    silent-failure case). Every other outcome (extraction_error,
    validation_failed) is acceptable — the parser correctly refuses
    non-statements. The result is written to a markdown table so the
    operator can read the empirical baseline after a run.
    """
    from aegis.llm import BedrockClient

    pdf_path = tmp_path / f"{fixture_name}.pdf"
    generator(pdf_path)
    pdf_bytes = pdf_path.read_bytes()
    assert pdf_bytes.startswith(b"%PDF-"), "fixture must be a real PDF"

    llm = BedrockClient()

    result = _FixtureResult(
        fixture=fixture_name,
        case="UNKNOWN",
        parse_status=None,
        extraction_error=None,
        validation_failures=[],
        extracted_summary=None,
        transaction_count=None,
    )

    try:
        pass1 = extract_statement(pdf_bytes, llm)
    except ExtractionError as exc:
        result.case = "A_or_B"
        result.parse_status = "extraction_error"
        result.extraction_error = str(exc)[:500]
    else:
        # Pass 1 succeeded — what does validation say?
        result.transaction_count = len(pass1.statement.transactions)
        result.extracted_summary = {
            "beginning_balance": str(pass1.statement.summary.beginning_balance),
            "ending_balance": str(pass1.statement.summary.ending_balance),
            "deposit_total": str(pass1.statement.summary.deposit_total),
            "withdrawal_total": str(pass1.statement.summary.withdrawal_total),
            "period_start": str(pass1.statement.summary.period_start),
            "period_end": str(pass1.statement.summary.period_end),
        }
        vr = validate_extraction(pass1.statement, today=date(2026, 6, 1))
        if not vr.passed:
            result.case = "C-2_or_validation_caught"
            result.parse_status = "validation_failed"
            result.validation_failures = list(vr.failures)
        else:
            # **The dangerous case.** Validation passed on a non-statement.
            result.case = "C-1_silent_failure"
            result.parse_status = "passes_validation"

    # Surface the captured result to stdout (pytest -s shows it)
    print(f"\n=== {fixture_name} ===")
    print(f"  case: {result.case}")
    print(f"  parse_status: {result.parse_status}")
    if result.extraction_error:
        print(f"  extraction_error: {result.extraction_error[:200]}")
    if result.validation_failures:
        print(f"  validation_failures: {result.validation_failures}")
    if result.extracted_summary:
        print(f"  extracted_summary: {json.dumps(result.extracted_summary)}")
    if result.transaction_count is not None:
        print(f"  transaction_count: {result.transaction_count}")

    # The hard assertion. If this fails on any fixture, that's the C-1
    # case we need the validation guard to catch.
    assert result.parse_status != "passes_validation", (
        f"{fixture_name} passed validation as a 'statement' — case C-1, "
        f"silent failure. Validation guard required. "
        f"Extracted summary: {result.extracted_summary}, "
        f"transactions: {result.transaction_count}"
    )
