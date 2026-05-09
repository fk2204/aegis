"""Synthetic bank statement corpus generator (Phase 5.5).

Outputs deterministic PDF + JSON manifest pairs to
``tests/fixtures/corpus/synthetic/``. Each manifest is the **ground truth
the PDF was generated from** — never extracted from the PDF after the
fact. The Phase 5.5 corpus runner asserts the parser+scorer+disclosure
pipeline reproduces these numbers within explicit per-metric tolerance.

Usage::

    python -m scripts.generate_corpus              # write all PDFs + manifests
    python -m scripts.generate_corpus --clean      # delete existing first
    python -m scripts.generate_corpus --dry-run    # print plan, write nothing

Determinism
-----------
Every (bank, scenario) pair has a fixed seed so the same generator
invocation always produces byte-identical PDFs. Random walk inside a
scenario uses a per-pair ``random.Random(seed)``, never the global RNG.

Supported banks (initial set)
-----------------------------
- ``chase_business``      — Chase Business Banking layout
- ``boa_business``        — Bank of America Business Advantage layout

Supported scenarios (initial set)
---------------------------------
- ``clean_profitable``    — healthy revenue, no NSF, ~30% margin
- ``nsf_heavy``           — multiple NSF fees, low ending balance
- ``mca_stacked``         — two existing MCA daily debits
- ``math_tampered``       — printed totals don't match transactions

Adding a new (bank, scenario) pair: register a row in ``CORPUS_RECIPES``
with a unique seed. The generator picks it up on the next invocation.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

CORPUS_DIR: Final[Path] = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "corpus" / "synthetic"
)

MANIFEST_VERSION: Final[str] = "1"

# --- domain types -----------------------------------------------------------


@dataclass
class SyntheticTx:
    """One transaction in the synthetic statement."""

    posted_date: date
    description: str
    amount: Decimal  # signed: deposits positive, withdrawals negative
    running_balance: Decimal
    category: str  # one of TransactionCategory
    source_page: int = 1
    source_line: int = 0  # filled when laid out

    def to_manifest(self) -> dict[str, Any]:
        return {
            "posted_date": self.posted_date.isoformat(),
            "description": self.description,
            "amount": str(self.amount),
            "running_balance": str(self.running_balance),
            "source_page": self.source_page,
            "source_line": self.source_line,
            "category": self.category,
        }


@dataclass
class SyntheticStatement:
    """Everything needed to render one synthetic PDF + manifest."""

    bank: str
    scenario: str
    seed: int
    period_start: date
    period_end: date
    beginning_balance: Decimal
    ending_balance: Decimal
    deposit_total: Decimal  # printed total (may differ from sum if math_tampered)
    withdrawal_total: Decimal  # printed total (signed negative)
    transactions: list[SyntheticTx]
    expected: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return f"{self.scenario}_{self.bank}_{self.seed:05d}"


# --- scenario builders ------------------------------------------------------


def _scenario_clean_profitable(rng: random.Random, period: tuple[date, date]) -> SyntheticStatement:
    start, end = period
    beginning = Decimal("5000.00")
    transactions: list[SyntheticTx] = []
    balance = beginning

    # Daily small expenses + 2-3 weekly deposits.
    cur = start
    while cur <= end:
        if cur.weekday() == 0:  # Monday — customer deposit batch
            amt = Decimal(rng.randrange(1500, 4000)).quantize(Decimal("0.01"))
            balance = (balance + amt).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(
                    cur, "ACH DEPOSIT CUSTOMER PAYMENTS",
                    amt, balance, "ach_credit",
                )
            )
        if cur.weekday() in (1, 3):  # Tue/Thu — operating expense
            amt = -Decimal(rng.randrange(100, 600)).quantize(Decimal("0.01"))
            balance = (balance + amt).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "VENDOR PAYMENT", amt, balance, "fee")
            )
        if cur.day == 15 and cur >= start:  # mid-month payroll
            amt = -Decimal("3500.00")
            balance = (balance + amt).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "PAYROLL ADP", amt, balance, "payroll")
            )
        cur += timedelta(days=1)

    deposits = sum((t.amount for t in transactions if t.amount > 0), Decimal("0.00"))
    withdrawals_signed = sum(
        (t.amount for t in transactions if t.amount < 0), Decimal("0.00")
    )
    # Printed withdrawal_total is positive (the validator compares against
    # abs(sum of negatives)). Ending balance still uses the signed value.
    withdrawals = -withdrawals_signed
    ending = (beginning + deposits + withdrawals_signed).quantize(Decimal("0.01"))

    return SyntheticStatement(
        bank="",  # filled by caller
        scenario="clean_profitable",
        seed=0,
        period_start=start,
        period_end=end,
        beginning_balance=beginning,
        ending_balance=ending,
        deposit_total=deposits,
        withdrawal_total=withdrawals,
        transactions=transactions,
        expected={
            "validation_passed": True,
            "recommendation": "approve",
            "fraud_score": {"max": 25},
            "hard_decline_reasons": [],
        },
    )


def _scenario_nsf_heavy(rng: random.Random, period: tuple[date, date]) -> SyntheticStatement:
    start, end = period
    beginning = Decimal("2000.00")
    transactions: list[SyntheticTx] = []
    balance = beginning

    cur = start
    nsf_count = 0
    while cur <= end:
        if cur.weekday() == 0:
            amt = Decimal(rng.randrange(800, 1800)).quantize(Decimal("0.01"))
            balance = (balance + amt).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "ACH DEPOSIT", amt, balance, "ach_credit")
            )
        if cur.weekday() in (2, 4):
            amt = -Decimal(rng.randrange(400, 1200)).quantize(Decimal("0.01"))
            balance = (balance + amt).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "DEBIT CARD POS", amt, balance, "fee")
            )
        # NSF roughly weekly
        if cur.weekday() == 4 and rng.random() < 0.7:
            amt = -Decimal("35.00")
            balance = (balance + amt).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "NSF FEE INSUFFICIENT FUNDS", amt, balance, "nsf_fee")
            )
            nsf_count += 1
        cur += timedelta(days=1)

    deposits = sum((t.amount for t in transactions if t.amount > 0), Decimal("0.00"))
    withdrawals_signed = sum(
        (t.amount for t in transactions if t.amount < 0), Decimal("0.00")
    )
    # Printed withdrawal_total is positive (the validator compares against
    # abs(sum of negatives)). Ending balance still uses the signed value.
    withdrawals = -withdrawals_signed
    ending = (beginning + deposits + withdrawals_signed).quantize(Decimal("0.01"))

    return SyntheticStatement(
        bank="",
        scenario="nsf_heavy",
        seed=0,
        period_start=start,
        period_end=end,
        beginning_balance=beginning,
        ending_balance=ending,
        deposit_total=deposits,
        withdrawal_total=withdrawals,
        transactions=transactions,
        expected={
            "validation_passed": True,
            "recommendation": "decline" if nsf_count >= 10 else "refer",
            "num_nsf": nsf_count,
            "fraud_score": {"min": 25, "max": 80},
        },
    )


def _scenario_mca_stacked(rng: random.Random, period: tuple[date, date]) -> SyntheticStatement:
    """Two MCA daily debits = stacking. Triggers mca_positions=2 detection."""
    start, end = period
    beginning = Decimal("8000.00")
    transactions: list[SyntheticTx] = []
    balance = beginning

    daily_mca_a = Decimal("250.00")
    daily_mca_b = Decimal("180.00")

    cur = start
    while cur <= end:
        # Weekday daily MCA debits (skip weekends).
        if cur.weekday() < 5:
            balance = (balance - daily_mca_a).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "ACH DEBIT MCA FUNDER ALPHA", -daily_mca_a, balance, "mca_debit")
            )
            balance = (balance - daily_mca_b).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "ACH DEBIT MCA FUNDER BETA", -daily_mca_b, balance, "mca_debit")
            )
        if cur.weekday() == 0:
            amt = Decimal(rng.randrange(2500, 4500)).quantize(Decimal("0.01"))
            balance = (balance + amt).quantize(Decimal("0.01"))
            transactions.append(
                SyntheticTx(cur, "ACH DEPOSIT", amt, balance, "ach_credit")
            )
        cur += timedelta(days=1)

    deposits = sum((t.amount for t in transactions if t.amount > 0), Decimal("0.00"))
    withdrawals_signed = sum(
        (t.amount for t in transactions if t.amount < 0), Decimal("0.00")
    )
    # Printed withdrawal_total is positive (the validator compares against
    # abs(sum of negatives)). Ending balance still uses the signed value.
    withdrawals = -withdrawals_signed
    ending = (beginning + deposits + withdrawals_signed).quantize(Decimal("0.01"))

    return SyntheticStatement(
        bank="",
        scenario="mca_stacked",
        seed=0,
        period_start=start,
        period_end=end,
        beginning_balance=beginning,
        ending_balance=ending,
        deposit_total=deposits,
        withdrawal_total=withdrawals,
        transactions=transactions,
        expected={
            "validation_passed": True,
            "mca_positions_min": 2,
            "fraud_score": {"min": 30, "max": 90},
            "recommendation": "decline",
        },
    )


def _scenario_math_tampered(rng: random.Random, period: tuple[date, date]) -> SyntheticStatement:
    """Printed deposit_total in the summary lies — sum of rows doesn't match."""
    base = _scenario_clean_profitable(rng, period)
    # Inflate the printed deposit_total by $5000 — the validator must catch this.
    base.scenario = "math_tampered"
    base.deposit_total = (base.deposit_total + Decimal("5000.00")).quantize(Decimal("0.01"))
    base.expected = {
        "validation_passed": False,
        "expected_failure_substring": "reconciliation_failed",
        "recommendation": "manual_review",
    }
    return base


_ScenarioBuilder = Callable[[random.Random, tuple[date, date]], SyntheticStatement]

_SCENARIO_BUILDERS: Final[dict[str, _ScenarioBuilder]] = {
    "clean_profitable": _scenario_clean_profitable,
    "nsf_heavy": _scenario_nsf_heavy,
    "mca_stacked": _scenario_mca_stacked,
    "math_tampered": _scenario_math_tampered,
}


# --- bank renderers ---------------------------------------------------------


@dataclass
class BankLayout:
    name: str
    display_name: str
    header_color: tuple[float, float, float]


CHASE = BankLayout("chase_business", "Chase Business", (0.0, 0.36, 0.65))
BOA = BankLayout("boa_business", "Bank of America Business Advantage", (0.78, 0.05, 0.18))


def _render_pdf(statement: SyntheticStatement, layout: BankLayout, out_path: Path) -> None:
    """Render a synthetic statement to PDF using a bank-specific layout.

    Side effect: assigns ``source_page`` and ``source_line`` to each
    transaction as it's laid out, so the manifest matches the final PDF.
    """
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    width, height = LETTER

    # --- header --------------------------------------------------------------
    c.setFillColorRGB(*layout.header_color)
    c.rect(0, height - 1.0 * inch, width, 1.0 * inch, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(0.5 * inch, height - 0.6 * inch, layout.display_name)
    c.setFont("Helvetica", 10)
    c.drawString(
        0.5 * inch,
        height - 0.85 * inch,
        f"Statement period: {statement.period_start} to {statement.period_end}",
    )

    # --- summary box ---------------------------------------------------------
    c.setFillColor(colors.black)
    box_top = height - 1.5 * inch
    c.setFont("Helvetica-Bold", 12)
    c.drawString(0.5 * inch, box_top, "Account Summary")
    c.setFont("Helvetica", 10)
    summary_lines = [
        f"Beginning balance: ${statement.beginning_balance}",
        f"Total deposits and additions: ${statement.deposit_total}",
        f"Total withdrawals: ${statement.withdrawal_total}",
        f"Ending balance: ${statement.ending_balance}",
    ]
    for i, line in enumerate(summary_lines):
        c.drawString(0.5 * inch, box_top - (0.18 * inch * (i + 1)), line)

    # --- transactions table --------------------------------------------------
    table_top = box_top - 1.5 * inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(0.5 * inch, table_top, "Date")
    c.drawString(1.2 * inch, table_top, "Description")
    c.drawRightString(5.3 * inch, table_top, "Amount")
    c.drawRightString(7.0 * inch, table_top, "Balance")
    c.line(0.5 * inch, table_top - 0.05 * inch, 7.0 * inch, table_top - 0.05 * inch)

    c.setFont("Helvetica", 9)
    row_y = table_top - 0.25 * inch
    page_num = 1
    line_in_page = 1
    for tx in statement.transactions:
        if row_y < 0.75 * inch:
            c.showPage()
            page_num += 1
            line_in_page = 1
            c.setFont("Helvetica", 9)
            row_y = height - 1.0 * inch

        tx.source_page = page_num
        tx.source_line = line_in_page

        c.drawString(0.5 * inch, row_y, tx.posted_date.isoformat())
        c.drawString(1.2 * inch, row_y, tx.description[:48])
        c.drawRightString(5.3 * inch, row_y, f"${tx.amount}")
        c.drawRightString(7.0 * inch, row_y, f"${tx.running_balance}")
        row_y -= 0.18 * inch
        line_in_page += 1

    c.save()


# --- corpus recipes ---------------------------------------------------------


@dataclass(frozen=True)
class Recipe:
    bank: str
    scenario: str
    seed: int


CORPUS_RECIPES: Final[tuple[Recipe, ...]] = (
    # Chase x all four scenarios
    Recipe("chase_business", "clean_profitable", 10001),
    Recipe("chase_business", "nsf_heavy", 10002),
    Recipe("chase_business", "mca_stacked", 10003),
    Recipe("chase_business", "math_tampered", 10004),
    # BoA subset (will expand alongside more scenarios in a follow-up)
    Recipe("boa_business", "clean_profitable", 20001),
    Recipe("boa_business", "nsf_heavy", 20002),
)

_BANK_LAYOUTS: Final[dict[str, BankLayout]] = {
    "chase_business": CHASE,
    "boa_business": BOA,
}


# --- generator entrypoint ---------------------------------------------------


def _build_statement(recipe: Recipe) -> SyntheticStatement:
    rng = random.Random(recipe.seed)
    builder = _SCENARIO_BUILDERS[recipe.scenario]
    period = (date(2026, 4, 1), date(2026, 4, 30))
    stmt = builder(rng, period)
    stmt.bank = recipe.bank
    stmt.seed = recipe.seed
    return stmt


def _write_pair(stmt: SyntheticStatement, out_dir: Path) -> tuple[Path, Path]:
    pdf_path = out_dir / f"{stmt.slug}.pdf"
    manifest_path = out_dir / f"{stmt.slug}.manifest.json"

    layout = _BANK_LAYOUTS[stmt.bank]
    _render_pdf(stmt, layout, pdf_path)

    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "scenario": stmt.scenario,
        "bank": stmt.bank,
        "seed": stmt.seed,
        "summary": {
            "beginning_balance": str(stmt.beginning_balance),
            "ending_balance": str(stmt.ending_balance),
            "deposit_total": str(stmt.deposit_total),
            "withdrawal_total": str(stmt.withdrawal_total),
            "period_start": stmt.period_start.isoformat(),
            "period_end": stmt.period_end.isoformat(),
            "printed_transaction_count": len(stmt.transactions),
        },
        "transactions": [t.to_manifest() for t in stmt.transactions],
        "expected": stmt.expected,
        "tolerances": {"money": "1.00", "fraud_score": 5},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return pdf_path, manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="Delete existing PDFs/manifests first")
    parser.add_argument("--dry-run", action="store_true", help="Print plan, write nothing")
    args = parser.parse_args(argv)

    out_dir = CORPUS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.clean:
        for f in out_dir.iterdir():
            if f.suffix in {".pdf", ".json"}:
                f.unlink()

    print(f"output: {out_dir}")
    for recipe in CORPUS_RECIPES:
        stmt = _build_statement(recipe)
        if args.dry_run:
            print(f"  PLAN  {stmt.slug}: {len(stmt.transactions)} transactions")
            continue
        pdf_path, manifest_path = _write_pair(stmt, out_dir)
        print(
            f"  WROTE {stmt.slug}: {pdf_path.name} ({pdf_path.stat().st_size}B), "
            f"{manifest_path.name}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
