"""Toast POS sales CSV extractor.

Companion to ``csv_stripe.extract_stripe_csv`` and
``csv_square.extract_square_csv``. When the operator uploads a Toast
export (Toast → Reports → Export → choose date range), Toast ships a
structured CSV. Parsing it deterministically beats running a vision
pass: no Bedrock tokens, no LLM hallucination surface, exact source
attribution.

Toast vs. Stripe / Square — what's different
--------------------------------------------
Toast is a POS, not a payment processor. The export is a SALES log,
not a balance log. Practical implications:

* **No fees.** Toast doesn't surface processor fees in the export
  (those live on the merchant's processor statement — Toast partners
  with multiple processors). Every row's ``fee`` line item is
  ``$0.00`` and the aggregate ``fees_total`` is ``$0.00``.
* **No real payouts.** Toast doesn't write a transfer-to-bank row
  either. We synthesise a payout from the identity (gross - refund -
  chargeback - fee) so the validator's tie-out math holds. Same
  defensive posture as the Square extractor.
* **Voids are NOT chargebacks.** A void cancels a payment before it
  settles; it's a scratch entry, not a deduction from gross. We map
  voids to ``adjustment`` so they're excluded from the math identity
  AND don't inflate gross_charge. The dossier "voids + refunds" count
  is a separate concern (operator surface, not validator surface).
* **Cash payments are first-class.** ``Payment Type = "Cash"`` rows
  still map to ``gross_charge`` — they're revenue from the operator's
  perspective regardless of how the customer paid.

Toast CSV header (canonical)
----------------------------
Export from Toast → Reports → Export. Columns:

  Date, Server, Order ID, Order #, Location, Revenue Center, Tab Name,
  Item, Qty, Gross Amount, Discount Amount, Net Amount, Void,
  Void Reason, Check Amount, Tip Amount, Total Amount, Transaction Type,
  Payment Type, Last 4, Card Brand, Card Holder, Dining Options

The unique discriminator for Toast detection is the COMBINATION of
``Revenue Center`` AND ``Dining Options`` — neither Stripe nor Square
exports those columns. The CSV-header sniff in
``detect_processor_from_csv_header`` keys off this pair.

Quirks handled here
-------------------
* UTF-8 with BOM (``utf-8-sig``).
* Date format: Toast exports ``YYYY-MM-DD HH:MM:SS`` or the older
  ``MM/DD/YYYY HH:MM AM/PM``. Both are accepted.
* ``Void`` column accepts ``True``/``Yes``/``Y`` (case-insensitive)
  as truthy.
* Refund rows typically carry NEGATIVE ``Net Amount``. We take
  ``abs(Net Amount)`` because ``ProcessorLineItem.amount`` is
  non-negative; flow direction rides on ``kind``.
* ``Net Amount`` is post-discount but pre-tip. That's the revenue line
  we report as ``gross_charge`` — tips are pass-through to the server
  and not the merchant's revenue.
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


class ToastCsvError(RuntimeError):
    """Raised when the Toast CSV cannot be parsed into a statement."""


# Toast doesn't expose a "Type" column in the same vocabulary as Stripe
# or Square. The discriminator is the ``Transaction Type`` column +
# the ``Void`` flag. ``_TOAST_TRANSACTION_TYPE_MAP`` covers the
# documented values; unknown types fall through to ``adjustment``
# (excluded from the validation identity + aggregates) so a new Toast
# transaction type can never silently inflate revenue.
_TOAST_TRANSACTION_TYPE_MAP: Final[dict[str, ProcessorLineKind]] = {
    "payment": "gross_charge",
    "sale": "gross_charge",
    "refund": "refund",
    "chargeback": "chargeback",
    "dispute": "chargeback",
    "adjustment": "adjustment",
    "void": "adjustment",  # voids are scratch entries, not deductions
}

# Truthy spellings for the ``Void`` column. Toast historically wrote
# ``True``/``False`` but newer exports use ``Yes``/``No``; accept both.
_VOID_TRUTHY: Final[frozenset[str]] = frozenset({"true", "yes", "y", "1"})


# Hard cap on CSV size — matches Stripe/Square parsers + the PDF cap.
_MAX_CSV_BYTES: Final[int] = 25 * 1024 * 1024


# Required column set — the structural signature of a Toast export.
# ``Revenue Center`` + ``Dining Options`` are the Toast-unique
# discriminator pair. Trailing optional columns are tolerated by
# ``csv.DictReader``.
_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "Date",
        "Net Amount",
        "Transaction Type",
        "Revenue Center",
        "Dining Options",
    }
)


def extract_toast_csv(
    csv_bytes: bytes,
    *,
    business_name: str | None = None,
) -> ExtractedProcessorStatement:
    """Parse a Toast sales-export CSV into a statement.

    Parameters
    ----------
    csv_bytes
        Raw CSV file content. Caller is responsible for reading from
        disk and applying any size limit beyond the
        ``_MAX_CSV_BYTES`` defensive cap.
    business_name
        Optional merchant business name. Toast's CSV doesn't include
        the merchant name (it's on the export header, not in the
        row stream); the upload route can pass it through from the
        merchant row.

    Returns
    -------
    ExtractedProcessorStatement
        Validated Pydantic model with ``processor="toast"``, per-row
        source attribution, and a ``ProcessorSummary`` whose printed
        totals are SUMMED from the CSV rows (Toast's CSV doesn't print
        a separate summary block — the summed totals ARE the printed
        totals).

    Raises
    ------
    ToastCsvError
        On empty / oversized input, malformed CSV, missing required
        columns, or rows that fail Pydantic validation.
    """
    if len(csv_bytes) == 0:
        raise ToastCsvError("empty CSV buffer")
    if len(csv_bytes) > _MAX_CSV_BYTES:
        raise ToastCsvError(f"CSV buffer too large: {len(csv_bytes)} bytes (max {_MAX_CSV_BYTES})")

    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToastCsvError(f"CSV is not UTF-8: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise ToastCsvError("CSV has no header row")
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
        # Toast Dashboard prepends a header; csv.DictReader consumes
        # it before yielding data rows. csv_idx=0 → first DATA row,
        # which corresponds to printed page 1, line 2.
        posted = _parse_date(raw_row.get("Date"))
        kind = _map_kind(
            transaction_type=raw_row.get("Transaction Type"),
            void_flag=raw_row.get("Void"),
        )
        net_amount = _decimal_from_csv(raw_row.get("Net Amount"))
        description = _build_description(raw_row)
        if not description:
            description = kind

        main_item = _build_line_item(
            posted_date=posted,
            description=description[:500],
            kind=kind,
            amount=net_amount.copy_abs(),
            source_line=csv_idx + 2,
        )
        rows.append(main_item)
        summed_by_kind[kind] += main_item.amount
        min_date, max_date = _update_period(min_date, max_date, posted)

        # Toast doesn't report processor fees — no synthetic fee line
        # item to emit per row. The aggregate ``fees_total`` stays $0.
        # If a future Toast export starts surfacing fees, add the
        # synthetic-fee emission here mirroring the Stripe / Square
        # extractors.

    if not rows:
        raise ToastCsvError("CSV had a header but no data rows")
    if min_date is None or max_date is None:
        raise ToastCsvError("no usable Date values in CSV")

    # Toast doesn't emit a transfer-to-bank row. Derive a synthetic
    # payout from the identity so the validator's tie-out math holds.
    # Same posture as the Square extractor's synthetic-payout derivation.
    if summed_by_kind["payout"] == Decimal("0.00"):
        derived_payout = (
            summed_by_kind["gross_charge"]
            - summed_by_kind["refund"]
            - summed_by_kind["chargeback"]
            - summed_by_kind["fee"]
        )
        summed_by_kind["payout"] = derived_payout
        synthetic_payout = _build_line_item(
            posted_date=max_date,
            description="Toast net payout (derived from identity)",
            kind="payout",
            amount=derived_payout.copy_abs(),
            source_line=len(rows) + 2,
        )
        rows.append(synthetic_payout)

    summary = ProcessorSummary(
        processor="toast",
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
        raise ToastCsvError(f"CSV-built statement failed schema validation: {exc}") from exc


def _check_required_columns(fieldnames: Sequence[str] | None) -> None:
    """Confirm Toast's structural column set is present.

    Required: ``Date``, ``Net Amount``, ``Transaction Type``,
    ``Revenue Center``, ``Dining Options``. The last two are the
    Toast-unique pair the detector keys off; their presence here is
    redundant defense (detection already gated on them) but cheap.
    """
    cols = set(fieldnames or [])
    missing = _REQUIRED_COLUMNS - cols
    if missing:
        raise ToastCsvError(f"Toast CSV missing required columns: {sorted(missing)}")


def _map_kind(
    *,
    transaction_type: str | None,
    void_flag: str | None,
) -> ProcessorLineKind:
    """Collapse Toast's ``Transaction Type`` + ``Void`` to our kind.

    Void rows ALWAYS map to ``adjustment`` regardless of the printed
    transaction type — a voided payment is a scratch entry, not
    revenue. Unknown transaction types default to ``adjustment`` so
    they're excluded from the validation identity rather than poisoning
    gross_volume (same defensive posture as Stripe / Square).
    """
    if _is_void(void_flag):
        return "adjustment"
    if not transaction_type:
        return "adjustment"
    normalized = transaction_type.strip().lower()
    return _TOAST_TRANSACTION_TYPE_MAP.get(normalized, "adjustment")


def _is_void(value: str | None) -> bool:
    """Return True iff the ``Void`` cell is a truthy spelling."""
    if not value:
        return False
    return value.strip().lower() in _VOID_TRUTHY


def _build_description(raw_row: dict[str, str | None]) -> str:
    """Build a row description from Tab Name / Item / Location.

    Toast doesn't have a single "Description" column the way Stripe /
    Square do. We synthesise one from the rows that carry the most
    operator-meaningful context:
      - Tab Name + Item if both present
      - Item alone otherwise
      - Falls back to ``"<Transaction Type> @ <Location>"``
    """
    tab = (raw_row.get("Tab Name") or "").strip()
    item = (raw_row.get("Item") or "").strip()
    location = (raw_row.get("Location") or "").strip()
    tx_type = (raw_row.get("Transaction Type") or "").strip()

    if tab and item:
        return f"{tab}: {item}"
    if item:
        return item
    if tab:
        return tab
    if tx_type and location:
        return f"{tx_type} @ {location}"
    if tx_type:
        return tx_type
    return ""


def _decimal_from_csv(value: str | None) -> Decimal:
    """Parse a Toast CSV money value into Decimal.

    Strips ``$``, ``,``, and whitespace. Empty cells → ``Decimal("0.00")``.
    """
    if value is None:
        return Decimal("0.00")
    stripped = value.strip().replace("$", "").replace(",", "")
    if not stripped:
        return Decimal("0.00")
    try:
        return Decimal(stripped)
    except (ValueError, ArithmeticError) as exc:
        raise ToastCsvError(f"could not parse CSV money value {value!r}: {exc}") from exc


def _parse_date(value: str | None) -> date:
    """Parse Toast's ``Date`` column.

    Toast's modern exports use ``YYYY-MM-DD HH:MM:SS``; older exports
    use ``MM/DD/YYYY HH:MM AM/PM``. ISO-only date strings (``YYYY-MM-DD``)
    are also accepted in case the operator's date range chops the time
    component off. We only need the date portion — Toast's per-row time
    doesn't affect daily aggregates.
    """
    if not value:
        raise ToastCsvError("row missing Date value")
    stripped = value.strip()
    # ISO date or datetime first.
    try:
        return date.fromisoformat(stripped[:10])
    except ValueError:
        pass
    # US datetime formats Toast historically used.
    for fmt in (
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    raise ToastCsvError(f"could not parse Date value {value!r}")


def _build_line_item(
    *,
    posted_date: date,
    description: str,
    kind: ProcessorLineKind,
    amount: Decimal,
    source_line: int,
) -> ProcessorLineItem:
    """Construct a validated line item.

    ``source_page`` is always 1 for CSV exports. ``source_line`` is the
    CSV row number (1-indexed, header = line 1).
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
        raise ToastCsvError(
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
    "ToastCsvError",
    "extract_toast_csv",
]
