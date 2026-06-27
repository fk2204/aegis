"""Clover transactions CSV extractor.

Companion to ``csv_stripe`` and ``csv_square`` — same output shape
(``ExtractedProcessorStatement``), same validation discipline (gross -
refunds - chargebacks - fees == payouts +/- $0.01).

When the operator exports the merchant's Clover Dashboard activity as a
CSV (Dashboard → Reports → Transactions → Export), Clover ships a
structured CSV. Parsing it deterministically beats running a vision
pass:

  * No Bedrock tokens.
  * No LLM hallucination surface.
  * Source attribution is exact (CSV row index).

Clover CSV header (canonical)
-----------------------------
Documented at Clover's help center under "Reports → Transactions →
Export". URL omitted because the parser makes no outbound calls to
clover.com; it operates only on operator-supplied CSV exports.

  Date & Time,Description,Amount,Tip,Tax,Total,Payment Type,Card Type,
  Last 4,Auth Code,Card Holder Name,Employee,Order ID,Device ID,Note

We require a structural subset (the discriminating columns + the math
columns). Trailing columns are tolerated — Clover has added optional
columns over time.

Quirks handled here
-------------------
* UTF-8 with BOM (``utf-8-sig``) — Clover Dashboard exports the BOM
  same as Stripe + Square.
* The ``Description`` column + ``Auth Code`` value + ``Amount`` sign
  drive the row's ``kind``. Clover lacks an explicit Transaction Type
  column (unlike Square's ``Event Type``); we discriminate via these
  signals:
    - ``Auth Code == "VOID"`` (or contains "VOID") → ``adjustment``
      (excluded from the identity — see CLAUDE.md scoring discipline;
      mapping a void to ``refund`` would double-count if the original
      Payment row was also present, and to ``gross_charge`` is just
      wrong).
    - ``Description`` contains "Refund" or "refund" → ``refund``.
    - ``Description`` contains "Adjustment" or "adjustment" → ``adjustment``.
    - ``Amount < 0`` → ``refund`` (Clover refunds are sometimes
      printed as negative Amounts in addition to the Description hint).
    - Otherwise → ``gross_charge``.
* ``ProcessorLineItem.amount`` is non-negative. We take ``abs(Amount)``.
* Clover does NOT report processor fees in this export — fees come
  from a separate processor statement. We do NOT emit synthetic fee
  line items here. ``fees_total`` in the summary is $0.00; the
  validator identity (``gross - refund - chargeback - fee == payout``)
  reduces to ``gross - refund - chargeback == payout`` and the
  synthetic payout derivation follows from that simplified identity.
* Date format is ``YYYY-MM-DD HH:MM:SS`` typically; ``MM/DD/YYYY``
  fallback is accepted for older exports.
* Cash payments arrive with empty ``Card Type`` / ``Last 4`` / ``Auth
  Code`` — they still count as ``gross_charge`` (cash IS revenue;
  the Payment Type column distinguishes for the dossier's drill-down,
  not for the aggregate).
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


class CloverCsvError(RuntimeError):
    """Raised when the Clover CSV cannot be parsed into a statement."""


# Hard cap on CSV size. Mirrors the Stripe / Square caps; Clover's
# monthly exports for a typical SMB land well under 5 MB.
_MAX_CSV_BYTES: Final[int] = 25 * 1024 * 1024


# Required column set — the structural signature of a Clover CSV.
# We need every column we actually read; optional trailing columns
# Clover may have added are tolerated.
_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "Date & Time",
        "Description",
        "Amount",
        "Auth Code",
        "Device ID",
    }
)


def extract_clover_csv(
    csv_bytes: bytes,
    *,
    business_name: str | None = None,
) -> ExtractedProcessorStatement:
    """Parse a Clover transactions CSV into a statement.

    Parameters
    ----------
    csv_bytes
        Raw CSV file content. Caller is responsible for reading from
        disk and applying any size limit beyond the ``_MAX_CSV_BYTES``
        defensive cap.
    business_name
        Optional merchant business name. Clover's CSV doesn't include
        the merchant name in-band; the upload route can pass it
        through from the merchant row.

    Returns
    -------
    ExtractedProcessorStatement
        Validated Pydantic model with ``processor="clover"``, per-row
        source attribution, and a ``ProcessorSummary`` whose printed
        totals are SUMMED from the CSV rows (Clover's CSV doesn't
        print a separate summary block — the summed totals ARE the
        printed totals).

    Raises
    ------
    CloverCsvError
        On empty / oversized input, malformed CSV, missing required
        columns, or rows that fail Pydantic validation.
    """
    if len(csv_bytes) == 0:
        raise CloverCsvError("empty CSV buffer")
    if len(csv_bytes) > _MAX_CSV_BYTES:
        raise CloverCsvError(f"CSV buffer too large: {len(csv_bytes)} bytes (max {_MAX_CSV_BYTES})")

    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CloverCsvError(f"CSV is not UTF-8: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise CloverCsvError("CSV has no header row")
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
        # Clover's "Date & Time" column carries both — we extract just
        # the date component for ``posted_date``; the time stays in the
        # original row for audit drill-down but doesn't affect aggregates.
        posted = _parse_datetime(raw_row.get("Date & Time"))
        amount_raw = _decimal_from_csv(raw_row.get("Amount"))
        kind = _map_kind(
            description=raw_row.get("Description"),
            auth_code=raw_row.get("Auth Code"),
            amount=amount_raw,
        )
        description = (raw_row.get("Description") or "").strip() or kind

        line_item = _build_line_item(
            posted_date=posted,
            description=description[:500],
            kind=kind,
            amount=amount_raw.copy_abs(),
            source_line=csv_idx + 2,
        )
        rows.append(line_item)
        summed_by_kind[kind] += line_item.amount
        min_date, max_date = _update_period(min_date, max_date, posted)

    if not rows:
        raise CloverCsvError("CSV had a header but no data rows")
    if min_date is None or max_date is None:
        raise CloverCsvError("no usable Date & Time values in CSV")

    # Clover CSV exports don't include payout rows — operator pulls a
    # separate payout report. We derive a synthetic payout from the
    # identity so the validator's tie-out math holds. Same pattern as
    # the Square extractor; with ``fee == 0`` for Clover, the identity
    # reduces to ``gross - refund - chargeback == payout``.
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
            description="Clover net payout (derived from identity)",
            kind="payout",
            amount=derived_payout.copy_abs(),
            source_line=len(rows) + 2,
        )
        rows.append(synthetic_payout)

    summary = ProcessorSummary(
        processor="clover",
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
        raise CloverCsvError(f"CSV-built statement failed schema validation: {exc}") from exc


def _check_required_columns(fieldnames: Sequence[str] | None) -> None:
    """Confirm Clover's discriminating + math column set is present."""
    cols = set(fieldnames or [])
    missing = _REQUIRED_COLUMNS - cols
    if missing:
        raise CloverCsvError(f"Clover CSV missing required columns: {sorted(missing)}")


def _map_kind(
    *,
    description: str | None,
    auth_code: str | None,
    amount: Decimal,
) -> ProcessorLineKind:
    """Derive a ``ProcessorLineKind`` from Clover's discriminative columns.

    Clover lacks an explicit Transaction Type column. The kind comes
    from a combination of Description text, Auth Code value, and Amount
    sign. Conservative defaults: anything that doesn't pattern-match
    a refund / void / adjustment is treated as ``gross_charge`` (the
    common case — Clover exports are dominated by Payment rows).

    Voids map to ``adjustment``, not ``refund``, on purpose: a Clover
    "Void" row typically pairs with a "Payment" row that was also in
    the export. Mapping to ``refund`` would net the same dollars out
    twice; mapping to ``adjustment`` keeps it out of the identity so
    the synthetic-payout derivation isn't fooled.
    """
    auth_normalized = (auth_code or "").strip().upper()
    description_normalized = (description or "").strip().lower()

    if auth_normalized == "VOID" or "VOID" in auth_normalized:
        return "adjustment"
    if "refund" in description_normalized:
        return "refund"
    if "adjustment" in description_normalized:
        return "adjustment"
    if "void" in description_normalized:
        return "adjustment"
    if amount < Decimal("0.00"):
        return "refund"
    return "gross_charge"


def _decimal_from_csv(value: str | None) -> Decimal:
    """Parse a Clover CSV money value into Decimal.

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
        raise CloverCsvError(f"could not parse CSV money value {value!r}: {exc}") from exc


def _parse_datetime(value: str | None) -> date:
    """Parse Clover's ``Date & Time`` column into a date.

    Standard format is ``YYYY-MM-DD HH:MM:SS``; ``MM/DD/YYYY HH:MM AM/PM``
    is accepted as a fallback for older exports. Pure-date values are
    also accepted (some exports drop the time component).
    """
    if not value:
        raise CloverCsvError("row missing Date & Time value")
    stripped = value.strip()
    # ISO with time component first.
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    # Date-only fallback.
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        pass
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    raise CloverCsvError(f"could not parse Date & Time value {value!r}")


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
    with the PDF parser and other CSV extractors.
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
        raise CloverCsvError(
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
    "CloverCsvError",
    "extract_clover_csv",
]
