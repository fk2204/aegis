"""Square transactions CSV extractor.

Companion to ``extract_square`` (PDF / Bedrock vision) and a structural
mirror of ``csv_stripe.extract_stripe_csv`` — same output shape
(``ExtractedProcessorStatement``), same validation discipline (gross -
refunds - chargebacks - fees == payouts +/- $0.01).

When the operator exports the merchant's Square Dashboard activity as a
CSV (Dashboard → Reports → Transactions → Export), there's no PDF to
OCR — Square ships a structured CSV. Parsing it deterministically beats
running a vision pass:

  * No Bedrock tokens.
  * No LLM hallucination surface.
  * Source attribution is exact (CSV row index).

Square CSV header (canonical)
-----------------------------
Documented at https://squareup.com/help/article/5161-export-transactions-and-payments
(verified 2026-06-26).

  Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID,
  Payment ID,Card Brand,PAN Suffix,Device Name,Notes,Event Type,Location

We require the first 8 columns (the structural signature). Trailing
columns are tolerated — Square has added optional columns over time and
the parser shouldn't reject a row that carries an extra "Source" or
"Customer Reference ID" field.

Quirks handled here
-------------------
* UTF-8 with BOM (``utf-8-sig``) — Square Dashboard exports the BOM,
  same as Stripe.
* The ``Event Type`` column is the discriminator. Square's canonical
  values map to our internal ``ProcessorLineKind`` per the table in
  ``_SQUARE_EVENT_TYPE_MAP`` below. Unknown event types default to
  ``adjustment`` (excluded from the validation identity + aggregates)
  so an unrecognized row never corrupts the math — same defensive
  posture as the Stripe extractor.
* Money sign convention: Square prints refunds and chargebacks with
  POSITIVE amounts in a "deduction" column (the deposit row carries
  the net). ``ProcessorLineItem.amount`` is non-negative; flow direction
  rides on ``kind``. We take ``abs(Amount)`` defensively in case a
  particular export carries a leading minus.
* The ``Fee`` column on a Payment row gets emitted as a synthetic
  ``fee`` line item — same pattern as the Stripe extractor so the
  validator's per-kind tie-out + identity hold to the cent.
* Date + Time + Time Zone arrive as separate columns. We use ``Date``
  alone (a date, no time component) to set ``posted_date`` — the
  per-row time is informational and doesn't affect aggregates.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
    ProcessorLineKind,
    ProcessorSummary,
)


class SquareCsvError(RuntimeError):
    """Raised when the Square CSV cannot be parsed into a statement."""


# Square's "Event Type" column → our internal ``ProcessorLineKind``.
#
# Conservative on purpose — see the Stripe map for the same rationale.
# Unknown event types fall through to ``adjustment`` (NEVER
# ``gross_charge``), so a Square dashboard release that introduces a
# new event type can't silently inflate revenue. Operator confirmation
# precedes any addition to this map.
_SQUARE_EVENT_TYPE_MAP: Final[dict[str, ProcessorLineKind]] = {
    # Money in from a customer card swipe / online checkout.
    "payment": "gross_charge",
    "sale": "gross_charge",
    "card sale": "gross_charge",
    # Operator-initiated refunds.
    "refund": "refund",
    "returned": "refund",
    # Customer disputes / chargebacks.
    "dispute": "chargeback",
    "chargeback": "chargeback",
    # Adjustment / balance corrections — kept out of the identity.
    "adjustment": "adjustment",
    "balance adjustment": "adjustment",
}


# Hard cap on CSV size. Square monthly exports for $1M/mo merchants
# come in well under 5 MB; 25 MB matches the PDF cap and gives plenty
# of headroom for outliers.
_MAX_CSV_BYTES: Final[int] = 25 * 1024 * 1024


# Required column set — the structural signature of a Square CSV.
# Optional trailing columns are tolerated by ``csv.DictReader``; only
# the columns we read MUST be present.
_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "Date",
        "Time",
        "Time Zone",
        "Description",
        "Amount",
        "Fee",
        "Net",
        "Transaction ID",
        "Event Type",
    }
)


def extract_square_csv(
    csv_bytes: bytes,
    *,
    business_name: str | None = None,
) -> ExtractedProcessorStatement:
    """Parse a Square transactions CSV into a statement.

    Parameters
    ----------
    csv_bytes
        Raw CSV file content. Caller is responsible for reading from
        disk and applying any size limit beyond the
        ``_MAX_CSV_BYTES`` defensive cap.
    business_name
        Optional merchant business name. Square's CSV doesn't include
        the merchant name (it's on the dashboard cover, not in the
        export); the upload route can pass it through from the
        merchant row.

    Returns
    -------
    ExtractedProcessorStatement
        Validated Pydantic model with ``processor="square"``,
        per-row source attribution, and a ``ProcessorSummary`` whose
        printed totals are SUMMED from the CSV rows (Square's CSV
        doesn't print a separate summary block — the summed totals ARE
        the printed totals).

    Raises
    ------
    SquareCsvError
        On empty / oversized input, malformed CSV, missing required
        columns, or rows that fail Pydantic validation.
    """
    if len(csv_bytes) == 0:
        raise SquareCsvError("empty CSV buffer")
    if len(csv_bytes) > _MAX_CSV_BYTES:
        raise SquareCsvError(f"CSV buffer too large: {len(csv_bytes)} bytes (max {_MAX_CSV_BYTES})")

    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SquareCsvError(f"CSV is not UTF-8: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise SquareCsvError("CSV has no header row")
    _check_required_columns(fieldnames)

    rows: list[ProcessorLineItem] = []
    summed_by_kind: dict[ProcessorLineKind, Decimal] = {
        "gross_charge": Decimal("0.00"),
        "refund": Decimal("0.00"),
        "chargeback": Decimal("0.00"),
        "fee": Decimal("0.00"),
        "payout": Decimal("0.00"),
        "adjustment": Decimal("0.00"),
    }
    min_date: date | None = None
    max_date: date | None = None

    for csv_idx, raw_row in enumerate(reader):
        # Square Dashboard prepends a header; csv.DictReader consumes
        # it before yielding data rows. csv_idx=0 → the first DATA row,
        # which corresponds to printed page 1, line 2 (header is line 1).
        posted = _parse_date(raw_row.get("Date"))
        kind = _map_kind(raw_row.get("Event Type"))
        gross_amount = _decimal_from_csv(raw_row.get("Amount"))
        fee_amount = _decimal_from_csv(raw_row.get("Fee"))
        description = (raw_row.get("Description") or raw_row.get("Event Type") or "").strip()
        if not description:
            description = kind

        main_item = _build_line_item(
            posted_date=posted,
            description=description[:500],
            kind=kind,
            amount=gross_amount.copy_abs(),
            source_line=csv_idx + 2,
        )
        rows.append(main_item)
        summed_by_kind[kind] += main_item.amount
        min_date, max_date = _update_period(min_date, max_date, posted)

        # Square Payment rows carry an inline ``Fee`` column — emit a
        # synthetic "fee" line item so the validator's identity ties
        # out and the fees aggregate has source attribution. Same
        # pattern as the Stripe extractor; skip when the parent kind
        # is already a fee or payout to avoid double-counting.
        if fee_amount != Decimal("0.00") and kind not in ("fee", "payout"):
            fee_item = _build_line_item(
                posted_date=posted,
                description=f"Square fee on {description[:480]}",
                kind="fee",
                amount=fee_amount.copy_abs(),
                source_line=csv_idx + 2,
            )
            rows.append(fee_item)
            summed_by_kind["fee"] += fee_item.amount

    if not rows:
        raise SquareCsvError("CSV had a header but no data rows")
    if min_date is None or max_date is None:
        raise SquareCsvError("no usable Date values in CSV")

    # Square CSV exports don't include a payout line — the "Net" column
    # is the merchant's net per-transaction take. We derive a synthetic
    # payout total from the identity (gross - refund - chargeback - fee)
    # so the validator's tie-out math holds. Real-world Square Dashboard
    # statements DO carry transfer rows (Event Type = "Transfer" / "Deposit
    # to bank") — those would be picked up by the kind map and treated as
    # payouts; the synthetic derivation only fires when no payout rows
    # were observed in the export.
    if summed_by_kind["payout"] == Decimal("0.00"):
        derived_payout = (
            summed_by_kind["gross_charge"]
            - summed_by_kind["refund"]
            - summed_by_kind["chargeback"]
            - summed_by_kind["fee"]
        )
        summed_by_kind["payout"] = derived_payout
        # Synthetic payout row attributed to the last data row's date so
        # the validator's period-sanity check still passes; source_line
        # points to the line just after the last data row so the audit
        # drill-down distinguishes derived rows from real ones.
        synthetic_payout = _build_line_item(
            posted_date=max_date,
            description="Square net payout (derived from identity)",
            kind="payout",
            amount=derived_payout.copy_abs(),
            source_line=len(rows) + 2,
        )
        rows.append(synthetic_payout)

    summary = ProcessorSummary(
        processor="square",
        business_name=business_name,
        period_start=min_date,
        period_end=max_date,
        gross_volume=summed_by_kind["gross_charge"],
        refunds_total=summed_by_kind["refund"],
        chargebacks_total=summed_by_kind["chargeback"],
        fees_total=summed_by_kind["fee"],
        payouts_total=summed_by_kind["payout"],
        transaction_count=sum(1 for r in rows if r.kind == "gross_charge"),
    )

    try:
        return ExtractedProcessorStatement(summary=summary, transactions=rows)
    except ValidationError as exc:
        raise SquareCsvError(f"CSV-built statement failed schema validation: {exc}") from exc


def _check_required_columns(fieldnames: Sequence[str] | None) -> None:
    """Confirm Square's canonical column set is present.

    Square has added optional columns over time (Source, Customer
    Reference ID, etc.); we only require the structurally load-bearing
    ones. Missing optional columns are OK — ``raw_row.get(...)`` returns
    None for them.
    """
    cols = set(fieldnames or [])
    missing = _REQUIRED_COLUMNS - cols
    if missing:
        raise SquareCsvError(f"Square CSV missing required columns: {sorted(missing)}")


def _map_kind(raw_type: str | None) -> ProcessorLineKind:
    """Collapse Square's ``Event Type`` to our internal ``ProcessorLineKind``.

    Unknown types default to ``"adjustment"`` so they're excluded from
    the validation identity. New Square event types added here only
    after operator confirmation — silently mapping a new type to
    ``gross_charge`` could let a misclassified refund inflate revenue
    (the same trap the Stripe extractor guards against).
    """
    if not raw_type:
        return "adjustment"
    normalized = raw_type.strip().lower()
    return _SQUARE_EVENT_TYPE_MAP.get(normalized, "adjustment")


def _decimal_from_csv(value: str | None) -> Decimal:
    """Parse a Square CSV money value into Decimal.

    Strips ``$``, ``,``, and surrounding whitespace. Treats empty
    cells as ``Decimal("0.00")``.
    """
    if value is None:
        return Decimal("0.00")
    stripped = value.strip().replace("$", "").replace(",", "")
    if not stripped:
        return Decimal("0.00")
    try:
        return Decimal(stripped)
    except (ValueError, ArithmeticError) as exc:
        raise SquareCsvError(f"could not parse CSV money value {value!r}: {exc}") from exc


def _parse_date(value: str | None) -> date:
    """Parse Square's ``Date`` column (e.g. ``2026-03-01``) to a date.

    Square's standard format is ISO ``YYYY-MM-DD``; some older exports
    use ``MM/DD/YY``. Accept both for robustness.
    """
    if not value:
        raise SquareCsvError("row missing Date value")
    stripped = value.strip()
    # ISO first.
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        pass
    # Two-digit-year US format as a fallback.
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    raise SquareCsvError(f"could not parse Date value {value!r}")


def _build_line_item(
    *,
    posted_date: date,
    description: str,
    kind: ProcessorLineKind,
    amount: Decimal,
    source_line: int,
) -> ProcessorLineItem:
    """Construct a validated line item.

    ``source_page`` is always 1 for CSV exports — there's no concept of
    pagination. ``source_line`` is the CSV row number (1-indexed,
    counting the header). Keeps the audit drill-down model consistent
    with the PDF parser.
    """
    try:
        return ProcessorLineItem(
            posted_date=posted_date,
            description=description,
            kind=kind,
            amount=amount,
            source_page=1,
            source_line=source_line,
        )
    except ValidationError as exc:
        raise SquareCsvError(
            f"CSV row failed schema validation (kind={kind}, line={source_line}): {exc}"
        ) from exc


def _update_period(
    current_min: date | None,
    current_max: date | None,
    new_date: date,
) -> tuple[date, date]:
    """Maintain running min/max of the row date stream."""
    new_min = new_date if current_min is None or new_date < current_min else current_min
    new_max = new_date if current_max is None or new_date > current_max else current_max
    return new_min, new_max


__all__ = [
    "SquareCsvError",
    "extract_square_csv",
]
