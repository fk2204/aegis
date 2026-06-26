"""Stripe balance-transactions CSV extractor.

Companion to ``extract_stripe`` (PDF / Bedrock vision). When the
operator uploads a Stripe Dashboard export (Reports → Balance
transactions), there's no PDF to OCR — Stripe ships a structured
CSV. Parsing it deterministically beats running a vision pass:

  * No Bedrock tokens.
  * No LLM hallucination surface.
  * Source attribution is exact (CSV row index).

Output shape is identical to the PDF extractor's:
``ExtractedProcessorStatement`` so the downstream
``validate_processor`` + ``aggregate_processor`` stages run unchanged.

Stripe CSV quirks handled here
------------------------------
* UTF-8 with BOM (``utf-8-sig``) — Stripe Dashboard always writes the
  BOM, and Python's ``csv`` module would otherwise leak ``﻿``
  into the first column name.
* ``Type`` is the discriminator. Stripe's set is larger than ours
  (charge / payment / refund / payout / adjustment / stripe_fee /
  application_fee / fee / dispute / contribution / payout_failure …);
  we collapse it to the ``ProcessorLineKind`` literal.
* Balance-transaction ledger SIGN CONVENTION: payouts are negative
  (money leaving Stripe → merchant bank). We take ``abs(Amount)``
  because ``ProcessorLineItem.amount`` is non-negative; flow direction
  rides on ``kind``. The same applies to fees, refunds, and disputes.
* Empty / missing ``Net`` and ``Fee`` columns: zero them. Stripe omits
  ``Fee`` on payouts and ``Net`` on some adjustments.
* ``Created (UTC)`` is the canonical posting time. We map it to
  ``posted_date`` (UTC date). ``Available On (UTC)`` is informational
  and not surfaced in the line item.

Reference: https://docs.stripe.com/reports/balance-transaction-types
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

from pydantic import ValidationError

from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
    ProcessorLineKind,
    ProcessorSummary,
)


class StripeCsvError(RuntimeError):
    """Raised when the Stripe CSV cannot be parsed into a statement."""


# Stripe's "Type" column → our internal ``ProcessorLineKind``.
#
# The map is deliberate and conservative. New Stripe types that aren't
# listed default to ``"adjustment"`` (excluded from the validation
# identity + aggregates) so an unrecognized row never corrupts the
# math. New entries should be added explicitly after operator review.
_STRIPE_TYPE_MAP: Final[dict[str, ProcessorLineKind]] = {
    # Money in from a customer.
    "charge": "gross_charge",
    "payment": "gross_charge",
    # Operator-initiated refunds.
    "refund": "refund",
    "payment_refund": "refund",
    # Customer disputes / chargebacks.
    "dispute": "chargeback",
    "payment_failure_refund": "chargeback",
    # Stripe fees in their many shapes.
    "stripe_fee": "fee",
    "application_fee": "fee",
    "fee": "fee",
    "tax_fee": "fee",
    # Payout to the merchant's bank.
    "payout": "payout",
    "transfer": "payout",
    "payout_failure": "adjustment",
    # Misc balance corrections — kept out of the validator's identity.
    "adjustment": "adjustment",
    "contribution": "adjustment",
    "reserve_transaction": "adjustment",
    "topup": "adjustment",
}


# Hard cap on CSV size to keep memory predictable. Real Stripe monthly
# exports for $1M/mo merchants come in well under 5 MB; 25 MB matches
# the PDF cap and gives plenty of headroom for outliers.
_MAX_CSV_BYTES: Final[int] = 25 * 1024 * 1024


def extract_stripe_csv(
    csv_bytes: bytes,
    *,
    business_name: str | None = None,
) -> ExtractedProcessorStatement:
    """Parse a Stripe balance-transactions CSV into a statement.

    Parameters
    ----------
    csv_bytes
        Raw CSV file content. Caller is responsible for reading from
        disk and applying any size limit beyond the
        ``_MAX_CSV_BYTES`` defensive cap.
    business_name
        Optional merchant business name. Stripe's CSV doesn't include
        the merchant name (it's only in the PDF cover); the upload
        route can pass it through from the merchant row.

    Returns
    -------
    ExtractedProcessorStatement
        Validated Pydantic model with ``processor="stripe"``,
        per-row source attribution, and a ``ProcessorSummary``
        whose printed totals are SUMMED from the CSV rows (Stripe's
        CSV doesn't print a separate summary block — the summed
        totals ARE the printed totals).

    Raises
    ------
    StripeCsvError
        On empty / oversized input, malformed CSV, missing required
        columns, or rows that fail Pydantic validation.
    """
    if len(csv_bytes) == 0:
        raise StripeCsvError("empty CSV buffer")
    if len(csv_bytes) > _MAX_CSV_BYTES:
        raise StripeCsvError(f"CSV buffer too large: {len(csv_bytes)} bytes (max {_MAX_CSV_BYTES})")

    # Stripe dashboard writes UTF-8 with BOM. utf-8-sig strips it cleanly
    # without leaking ﻿ into the first column name.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StripeCsvError(f"CSV is not UTF-8: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    if not fieldnames:
        raise StripeCsvError("CSV has no header row")

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
        # Stripe Dashboard prepends a header row; csv.DictReader consumes
        # it before yielding data rows. csv_idx=0 → the first DATA row,
        # which corresponds to printed page 1, line 2 (header is line 1).
        posted = _parse_utc_date(raw_row.get("Created (UTC)") or raw_row.get("Created"))
        kind = _map_kind(raw_row.get("Type"))
        gross_amount = _decimal_from_csv(raw_row.get("Amount"))
        fee_amount = _decimal_from_csv(raw_row.get("Fee"))
        description = (raw_row.get("Description") or raw_row.get("Type") or "").strip()
        if not description:
            # ProcessorLineItem.description has min_length=1 — fall back
            # to the discriminator type so the row still validates.
            description = kind

        # 1) Add the main row using the gross "Amount" as the line's
        # signed value, but stored as abs() since amount is non-negative.
        # Sign convention: Stripe ledger signs payouts / fees / refunds /
        # disputes as NEGATIVE. We take abs() — kind carries the
        # direction.
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

        # 2) Stripe charges carry an inline ``Fee`` column — emit a
        # synthetic "fee" line item for it so the validation identity
        # ties out and the fees aggregate has source attribution.
        # Adjustment rows occasionally have ``Fee`` populated too;
        # only emit the synthetic line when the fee is non-zero AND
        # the parent kind isn't already a fee (avoids double-counting).
        if fee_amount != Decimal("0.00") and kind not in ("fee", "payout"):
            fee_item = _build_line_item(
                posted_date=posted,
                description=f"Stripe fee on {description[:480]}",
                kind="fee",
                amount=fee_amount.copy_abs(),
                source_line=csv_idx + 2,
            )
            rows.append(fee_item)
            summed_by_kind["fee"] += fee_item.amount

    if not rows:
        raise StripeCsvError("CSV had a header but no data rows")
    if min_date is None or max_date is None:
        # Defensive — at least one row succeeded above so this branch
        # is unreachable unless the row stream had no usable dates.
        raise StripeCsvError("no usable Created (UTC) values in CSV")

    summary = ProcessorSummary(
        processor="stripe",
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
        raise StripeCsvError(f"CSV-built statement failed schema validation: {exc}") from exc


_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset({"id", "Type", "Amount", "Created (UTC)"})


def _check_required_columns(fieldnames: Sequence[str] | None) -> None:
    """Confirm Stripe's canonical column set is present.

    Stripe occasionally adds optional columns (Customer ID, Transfer,
    Customer Email…). We require only the structurally load-bearing
    ones; missing optional columns are OK and ``raw_row.get(...)``
    returns None for them.
    """
    cols = set(fieldnames or [])
    missing = _REQUIRED_COLUMNS - cols
    if missing:
        # ``Created`` is the pre-2023 name for ``Created (UTC)``; older
        # exports use it. Accept either as a fallback so historic CSVs
        # don't fail here.
        if "Created (UTC)" in missing and "Created" in cols:
            missing = missing - {"Created (UTC)"}
        if missing:
            raise StripeCsvError(f"Stripe CSV missing required columns: {sorted(missing)}")


def _map_kind(raw_type: str | None) -> ProcessorLineKind:
    """Collapse Stripe's ``Type`` to our internal ``ProcessorLineKind``.

    Unknown types default to ``"adjustment"`` so they're excluded from
    the validation identity rather than poisoning gross_volume. New
    Stripe types added here only after operator confirmation —
    silently mapping a new type to ``gross_charge`` could let an
    LLM-misclassified refund inflate revenue.
    """
    if not raw_type:
        return "adjustment"
    normalized = raw_type.strip().lower()
    return _STRIPE_TYPE_MAP.get(normalized, "adjustment")


def _decimal_from_csv(value: str | None) -> Decimal:
    """Parse a Stripe CSV money value into Decimal.

    Strips ``$``, ``,``, and surrounding whitespace. Treats empty
    cells as ``Decimal("0.00")`` — Stripe omits ``Fee`` on payouts and
    similar edge cases.
    """
    if value is None:
        return Decimal("0.00")
    stripped = value.strip().replace("$", "").replace(",", "")
    if not stripped:
        return Decimal("0.00")
    try:
        return Decimal(stripped)
    except (ValueError, ArithmeticError) as exc:
        raise StripeCsvError(f"could not parse CSV money value {value!r}: {exc}") from exc


def _parse_utc_date(value: str | None) -> date:
    """Parse Stripe's ``Created (UTC)`` timestamp to a UTC date.

    Stripe emits ISO-8601 like ``2026-03-15 14:22:09`` (space, no
    timezone suffix because the column header says UTC). We accept
    a ``T`` separator too in case Stripe normalizes it later.
    """
    if not value:
        raise StripeCsvError("row missing Created (UTC) value")
    stripped = value.strip()
    # Normalize space → T for fromisoformat.
    if " " in stripped and "T" not in stripped:
        stripped = stripped.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(stripped)
    except ValueError as exc:
        # Some Stripe exports include a millisecond suffix Python's
        # fromisoformat rejected prior to 3.11; defensive parse via
        # strptime as a fallback for older Python — AEGIS targets 3.12+
        # but the fallback costs nothing.
        try:
            dt = datetime.strptime(stripped, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            raise StripeCsvError(f"could not parse Created (UTC) timestamp {value!r}") from exc
    # Ensure timezone-naive values are treated as UTC (Stripe says so
    # in the column header). Timezone-aware values are converted.
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(UTC).date()


def _build_line_item(
    *,
    posted_date: date,
    description: str,
    kind: ProcessorLineKind,
    amount: Decimal,
    source_line: int,
) -> ProcessorLineItem:
    """Construct a validated line item.

    ``source_page`` is always 1 for CSV exports — there's no concept
    of pagination. ``source_line`` is the CSV row number (1-indexed,
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
        raise StripeCsvError(
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
    "StripeCsvError",
    "extract_stripe_csv",
]
