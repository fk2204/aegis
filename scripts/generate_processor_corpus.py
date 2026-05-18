"""Synthetic processor-statement corpus generator (mp Phase 6.6 / Stage 2C).

Outputs deterministic PDF + JSON manifest pairs to
``tests/fixtures/corpus/processor/``. Each manifest is the ground
truth the PDF was generated from — never extracted from the PDF
after the fact (per .claude/rules/testing.md). The corpus test asserts
the processor pipeline reproduces these numbers within ±$0.01.

Usage::

    python -m scripts.generate_processor_corpus              # write all
    python -m scripts.generate_processor_corpus --clean      # delete existing first
    python -m scripts.generate_processor_corpus --dry-run    # print plan, write nothing

Determinism is critical: ``reportlab`` emits ``/CreationDate`` +
``/ModDate`` by default which would change every run. ``invariant=True``
on ``canvas.Canvas`` strips those, so consecutive ``--clean`` runs
produce byte-identical PDFs and SHA-256 hashes match.

Scenarios per processor (Stripe, Square):
    - clean              healthy month, no chargebacks, ~3% fees
    - low_chargebacks    1 chargeback at <1% ratio (still proceed)
    - high_chargebacks   2-3 chargebacks at >2% ratio (review flag)
    - math_tampered      printed summary doesn't match line items
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

ProcessorBrand = Literal["stripe", "square"]
Scenario = Literal["clean", "low_chargebacks", "high_chargebacks", "math_tampered"]


# Per-(processor, scenario) seed table. Same shape as the bank corpus
# generator. New combinations get a new line; never recycle a seed
# because that would create accidental collisions when the recipe
# changes later.
@dataclass(frozen=True)
class Recipe:
    processor: ProcessorBrand
    scenario: Scenario
    seed: int


CORPUS_RECIPES: Final[tuple[Recipe, ...]] = (
    Recipe("stripe", "clean", seed=2001),
    Recipe("stripe", "low_chargebacks", seed=2002),
    Recipe("stripe", "high_chargebacks", seed=2003),
    Recipe("stripe", "math_tampered", seed=2004),
    Recipe("square", "clean", seed=2101),
    Recipe("square", "low_chargebacks", seed=2102),
    Recipe("square", "high_chargebacks", seed=2103),
    Recipe("square", "math_tampered", seed=2104),
)

CORPUS_DIR: Final[Path] = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "corpus"
    / "processor"
)

_PERIOD_START: Final[date] = date(2026, 1, 1)
_PERIOD_END: Final[date] = date(2026, 1, 31)


@dataclass
class Generated:
    """One generated row before it lands on the page."""

    posted_date: date
    description: str
    kind: str  # ProcessorLineKind from the parser model; kept loose here
    amount: Decimal


def _generate_lines(recipe: Recipe) -> list[Generated]:
    """Build the deterministic line-item set for this recipe.

    Layout:
        - Daily sales (gross_charge) spread across the period.
        - Per-charge processor fee (~2.9% + $0.30, capped at the
          gross). Square uses a flat 2.6% in the printed totals; we
          collapse both into a fixed ~3% effective rate for the
          synthetic corpus so the math is exactly reproducible.
        - Refunds: scenario-dependent count.
        - Chargebacks: scenario-dependent count + ratio.
        - One payout per Monday/Tuesday equal to the running net.
    """
    rng = random.Random(recipe.seed)
    rows: list[Generated] = []

    days = (_PERIOD_END - _PERIOD_START).days + 1
    # Target gross volume scaled per processor for variety.
    daily_targets = [
        Decimal(rng.choice([180, 215, 240, 275, 310, 345])).quantize(Decimal("0.01"))
        for _ in range(days)
    ]
    fee_rate = Decimal("0.029")  # 2.9%, common to Stripe + Square headline

    gross_total = Decimal("0.00")
    fees_total = Decimal("0.00")

    for i, target in enumerate(daily_targets):
        d = _PERIOD_START + timedelta(days=i)
        # 3-5 charges per day, summing to roughly the target.
        n_charges = rng.randint(3, 5)
        per_charge = (target / n_charges).quantize(Decimal("0.01"))
        # Adjust last charge to absorb rounding so the day's sum is exact.
        for j in range(n_charges):
            if j == n_charges - 1:
                amount = (target - per_charge * (n_charges - 1)).quantize(Decimal("0.01"))
            else:
                amount = per_charge
            fee = (amount * fee_rate).quantize(Decimal("0.01"))
            rows.append(
                Generated(
                    posted_date=d,
                    description=f"Card sale #{i * 10 + j + 1}",
                    kind="gross_charge",
                    amount=amount,
                )
            )
            rows.append(
                Generated(
                    posted_date=d,
                    description=f"Processing fee #{i * 10 + j + 1}",
                    kind="fee",
                    amount=fee,
                )
            )
            gross_total += amount
            fees_total += fee

    # Refunds.
    refund_count = {
        "clean": 0,
        "low_chargebacks": 1,
        "high_chargebacks": 2,
        "math_tampered": 1,
    }[recipe.scenario]
    refunds_total = Decimal("0.00")
    for k in range(refund_count):
        d = _PERIOD_START + timedelta(days=rng.randint(0, days - 1))
        amount = Decimal(rng.choice([45, 60, 75, 90])).quantize(Decimal("0.01"))
        rows.append(
            Generated(
                posted_date=d,
                description=f"Refund #{k + 1}",
                kind="refund",
                amount=amount,
            )
        )
        refunds_total += amount

    # Chargebacks (drive the chargeback_ratio flag).
    chargeback_count = {
        "clean": 0,
        "low_chargebacks": 1,
        "high_chargebacks": 3,
        "math_tampered": 0,
    }[recipe.scenario]
    chargebacks_total = Decimal("0.00")
    for k in range(chargeback_count):
        d = _PERIOD_START + timedelta(days=rng.randint(0, days - 1))
        amount = Decimal(rng.choice([85, 120, 150, 175])).quantize(Decimal("0.01"))
        rows.append(
            Generated(
                posted_date=d,
                description=f"Chargeback #{k + 1}",
                kind="chargeback",
                amount=amount,
            )
        )
        chargebacks_total += amount

    # Payouts: weekly settlement to bank. The total must equal
    # gross - refunds - chargebacks - fees so the validator's identity
    # holds. We compute once and emit a single payout row per week
    # to keep the synthetic statement compact.
    net = gross_total - refunds_total - chargebacks_total - fees_total
    weekly_payout = (net / 4).quantize(Decimal("0.01"))
    remaining = net
    for week in range(4):
        amount = weekly_payout if week < 3 else remaining
        remaining -= amount
        rows.append(
            Generated(
                posted_date=_PERIOD_START + timedelta(days=7 * (week + 1) - 1),
                description=f"Payout to bank #{week + 1}",
                kind="payout",
                amount=amount,
            )
        )

    # Sort by date then by kind ordering so the line items appear in a
    # natural reading order on the synthetic statement.
    kind_order = {"gross_charge": 0, "refund": 1, "chargeback": 2, "fee": 3, "payout": 4}
    rows.sort(key=lambda r: (r.posted_date, kind_order.get(r.kind, 99)))
    return rows


def _printed_totals(
    rows: list[Generated], scenario: Scenario
) -> dict[str, Decimal]:
    """Compute the printed-summary totals.

    ``math_tampered`` deliberately injects a $50 mismatch between the
    printed gross_volume and the summed gross_charge rows so the
    validator's tie-out catches it (the parser must NOT silently
    fudge — that's the firewall).
    """
    totals = {
        "gross_volume": Decimal("0.00"),
        "refunds_total": Decimal("0.00"),
        "chargebacks_total": Decimal("0.00"),
        "fees_total": Decimal("0.00"),
        "payouts_total": Decimal("0.00"),
    }
    for r in rows:
        if r.kind == "gross_charge":
            totals["gross_volume"] += r.amount
        elif r.kind == "refund":
            totals["refunds_total"] += r.amount
        elif r.kind == "chargeback":
            totals["chargebacks_total"] += r.amount
        elif r.kind == "fee":
            totals["fees_total"] += r.amount
        elif r.kind == "payout":
            totals["payouts_total"] += r.amount
    if scenario == "math_tampered":
        # Print an inflated gross — line items still sum correctly,
        # but the printed total disagrees by $50.00. The validator
        # must catch this and route the doc to manual_review.
        totals["gross_volume"] += Decimal("50.00")
    return totals


def _build_pdf(recipe: Recipe, rows: list[Generated], out_path: Path) -> None:
    """Render the synthetic statement to PDF.

    ``invariant=True`` strips ``/CreationDate`` + ``/ModDate`` so two
    consecutive runs of this generator produce byte-identical PDFs
    (the SHA-256 hashes match). This is the testing-rules contract.
    """
    c = canvas.Canvas(str(out_path), pagesize=LETTER, invariant=True)
    width, height = LETTER

    # ---- Page 1: header + summary
    c.setFont("Helvetica-Bold", 14)
    brand_label = "Stripe" if recipe.processor == "stripe" else "Square"
    c.drawString(inch, height - inch, f"{brand_label} Monthly Statement")
    c.setFont("Helvetica", 9)
    # Signature lines for the detector.
    if recipe.processor == "stripe":
        c.drawString(inch, height - inch - 12, "stripe.com")
        c.drawString(inch, height - inch - 24, "Stripe, Inc.")
    else:
        c.drawString(inch, height - inch - 12, "squareup.com")
        c.drawString(inch, height - inch - 24, "Block, Inc.")

    # Period
    c.setFont("Helvetica", 11)
    c.drawString(
        inch,
        height - inch - 48,
        f"Period: {_PERIOD_START.isoformat()} to {_PERIOD_END.isoformat()}",
    )

    # Summary block (header phrase per brand for the detector).
    c.setFont("Helvetica-Bold", 12)
    summary_heading = "Activity summary" if recipe.processor == "stripe" else "Sales summary"
    c.drawString(inch, height - inch - 80, summary_heading)
    totals = _printed_totals(rows, recipe.scenario)
    c.setFont("Helvetica", 10)
    y = height - inch - 100
    for label, key in (
        ("Gross volume", "gross_volume"),
        ("Refunds", "refunds_total"),
        ("Chargebacks", "chargebacks_total"),
        ("Fees", "fees_total"),
        ("Payouts", "payouts_total"),
    ):
        # Brand-specific phrasing in the line labels (so the detector
        # has multiple hits).
        if recipe.processor == "stripe":
            display_label = f"{label}{' (Net volume)' if key == 'payouts_total' else ''}"
        else:
            display_label = f"{label}{' (Net total)' if key == 'payouts_total' else ''}"
        c.drawString(inch, y, display_label)
        c.drawRightString(width - inch, y, f"${totals[key]:,.2f}")
        y -= 14

    # ---- Page 2+: line items
    c.showPage()
    c.setFont("Helvetica-Bold", 11)
    c.drawString(inch, height - inch, "Transactions")
    c.setFont("Helvetica", 9)
    y = height - inch - 24
    line_no_in_page = 3
    page_no = 2
    for row in rows:
        if y < inch:
            c.showPage()
            c.setFont("Helvetica-Bold", 11)
            c.drawString(inch, height - inch, "Transactions (continued)")
            c.setFont("Helvetica", 9)
            y = height - inch - 24
            line_no_in_page = 3
            page_no += 1
        c.drawString(inch, y, row.posted_date.isoformat())
        c.drawString(inch + 80, y, row.description)
        c.drawString(inch + 250, y, row.kind)
        c.drawRightString(width - inch, y, f"${row.amount:,.2f}")
        y -= 12
        line_no_in_page += 1

    c.save()


def _write_manifest(recipe: Recipe, rows: list[Generated], pdf_path: Path) -> None:
    """Write the JSON manifest alongside the PDF.

    The manifest is the ground truth the corpus test grades against —
    NEVER reverse-engineered from the parser's output.
    """
    totals = _printed_totals(rows, recipe.scenario)
    expected_pass = recipe.scenario != "math_tampered"
    summed = {
        "gross_charge": sum(
            (r.amount for r in rows if r.kind == "gross_charge"), Decimal("0.00")
        ),
        "refund": sum((r.amount for r in rows if r.kind == "refund"), Decimal("0.00")),
        "chargeback": sum(
            (r.amount for r in rows if r.kind == "chargeback"), Decimal("0.00")
        ),
        "fee": sum((r.amount for r in rows if r.kind == "fee"), Decimal("0.00")),
        "payout": sum((r.amount for r in rows if r.kind == "payout"), Decimal("0.00")),
    }
    expected_chargeback_ratio = (
        (summed["chargeback"] / summed["gross_charge"])
        if summed["gross_charge"] > Decimal("0")
        else Decimal("0")
    )
    manifest = {
        "processor": recipe.processor,
        "scenario": recipe.scenario,
        "seed": recipe.seed,
        "period_start": _PERIOD_START.isoformat(),
        "period_end": _PERIOD_END.isoformat(),
        "printed_summary": {k: str(v) for k, v in totals.items()},
        "summed_line_items": {k: str(v) for k, v in summed.items()},
        "expected": {
            "validation_passed": expected_pass,
            "expected_parse_status": (
                "manual_review"
                if recipe.scenario == "math_tampered"
                else (
                    "review"
                    if recipe.scenario in ("low_chargebacks", "high_chargebacks")
                    and expected_chargeback_ratio > Decimal("0.01")
                    else "proceed"
                )
            ),
            "chargeback_ratio": str(expected_chargeback_ratio),
        },
    }
    manifest_path = pdf_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _generate_one(recipe: Recipe) -> Path:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    rows = _generate_lines(recipe)
    pdf_name = f"{recipe.processor}_{recipe.scenario}.pdf"
    pdf_path = CORPUS_DIR / pdf_name
    _build_pdf(recipe, rows, pdf_path)
    _write_manifest(recipe, rows, pdf_path)
    return pdf_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else "")
    parser.add_argument(
        "--clean", action="store_true", help="Delete CORPUS_DIR contents first."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan; write nothing."
    )
    args = parser.parse_args(argv)

    if args.clean and not args.dry_run and CORPUS_DIR.exists():
        shutil.rmtree(CORPUS_DIR)

    written: list[str] = []
    for recipe in CORPUS_RECIPES:
        if args.dry_run:
            print(f"[dry-run] would write {recipe.processor}_{recipe.scenario}.pdf")
            continue
        path = _generate_one(recipe)
        written.append(str(path.relative_to(CORPUS_DIR.parent.parent.parent)))
    if args.dry_run:
        return 0
    print(f"wrote {len(written)} processor-corpus PDFs:")
    for w in written:
        print(f"  - {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
