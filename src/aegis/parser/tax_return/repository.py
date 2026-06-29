"""TaxReturnRepository — Protocol + InMemory + Supabase implementations.

Persistence layer for the ``tax_returns`` table introduced by
migration 093. Mirrors the two-impl pattern of
``aegis.parser.processor.repository`` and
``aegis.funder_note_submissions.repository``:

  * :class:`TaxReturnRow` is a Pydantic model carrying every column
    the dossier-shape write path populates.
  * :class:`TaxReturnRepository` is the Protocol the worker / dossier
    route depends on.
  * :class:`InMemoryTaxReturnRepository` is the test / offline
    backend.
  * :class:`SupabaseTaxReturnRepository` is the production backend.
    Decimal columns travel as ``str`` so Postgres ``numeric(14,2)``
    receives exact text — never a binary-float round-trip.

Why a dedicated repository (CLAUDE.md "External-integration test
discipline")
---------------------------------------------------------------------
The 2026-06-05 chunk-B / chunk-C field-drop bug happened because the
in-memory document repository bypassed the Supabase mappers and the
new columns silently never round-tripped. The repository here keeps
mapper coverage symmetrical: both backends store and return the same
``TaxReturnRow``, and the structural-guard test in
``tests/parser/test_tax_return_repository.py`` asserts that field set
end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.parser.tax_return.models import Money, TaxFormType

_log = get_logger(__name__)


class TaxReturnWriteError(RuntimeError):
    """Raised when a tax_returns row could not be persisted.

    Mirrors ``ProcessorStatementWriteError`` / ``AuditWriteError`` —
    a write failure halts the caller rather than letting it ship a
    "extracted" audit row with no durable record.
    """


class TaxReturnRow(BaseModel):
    """Application view of a row in ``tax_returns``.

    All money fields are Optional because no single form populates
    every column. Schedule C has cogs / total_expenses but never
    officer_compensation; 1120 has officer_compensation but never
    partner_distributions. The repository writes whichever fields are
    populated and the Postgres column defaults to NULL on the rest.

    ``raw_extraction`` is the full sanitised dict the extractor
    produced — kept for audit so the operator can compare the
    extractor output to the form even after the dossier has been
    fully populated.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID = Field(default_factory=uuid4)
    merchant_id: UUID
    document_id: UUID | None = None
    form_type: TaxFormType
    tax_year: int = Field(ge=2000, le=2100)
    gross_receipts: Money | None = None
    net_income: Money | None = None
    total_assets: Money | None = None
    total_liabilities: Money | None = None
    officer_compensation: Money | None = None
    extracted_at: datetime | None = None
    raw_extraction: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class TaxReturnRepository(Protocol):
    def upsert(self, row: TaxReturnRow) -> TaxReturnRow:
        """Insert a tax_returns row. Re-uploads of the same
        (merchant_id, tax_year, form_type) tuple replace the row so
        the dossier always reads the latest extraction.
        """

    def list_for_merchant(self, merchant_id: UUID) -> list[TaxReturnRow]:
        """Return every tax_returns row for ``merchant_id``, sorted
        by ``tax_year`` descending. Empty list when the merchant has
        no tax returns on file. Powers the dossier "Tax Return
        Summary" section's YoY rendering.
        """


# ---------------------------------------------------------------------------
# In-memory implementation — tests + offline runs.
# ---------------------------------------------------------------------------


class InMemoryTaxReturnRepository:
    """Dict-backed tax_returns store. Tests + offline use."""

    def __init__(self) -> None:
        self._rows: dict[UUID, TaxReturnRow] = {}

    def upsert(self, row: TaxReturnRow) -> TaxReturnRow:
        # Natural-key dedup: same (merchant, year, form_type) replaces.
        # Iterates the existing rows once; the in-memory store is
        # bounded to a single merchant in test scenarios so this is
        # O(n) over a small n.
        for existing_id, existing in list(self._rows.items()):
            if (
                existing.merchant_id == row.merchant_id
                and existing.tax_year == row.tax_year
                and existing.form_type == row.form_type
            ):
                del self._rows[existing_id]
                break
        if row.extracted_at is None:
            row = row.model_copy(update={"extracted_at": datetime.now(UTC)})
        self._rows[row.id] = row
        return row

    def list_for_merchant(self, merchant_id: UUID) -> list[TaxReturnRow]:
        rows = [r for r in self._rows.values() if r.merchant_id == merchant_id]
        rows.sort(key=lambda r: r.tax_year, reverse=True)
        return rows


# ---------------------------------------------------------------------------
# Supabase implementation
# ---------------------------------------------------------------------------


class SupabaseTaxReturnRepository:
    """Persistence backed by Postgres ``tax_returns`` (migration 093)."""

    def upsert(self, row: TaxReturnRow) -> TaxReturnRow:
        payload = _row_to_payload(row)
        try:
            # Delete-then-insert because the natural key
            # (merchant, year, form_type) isn't backed by a UNIQUE
            # constraint in migration 093 — operators occasionally
            # upload an amended return with the same form/year that
            # SHOULD replace the prior row. The delete is scoped to
            # the same merchant + year + form so a parallel upload
            # for a DIFFERENT year stays untouched.
            (
                get_supabase()
                .table("tax_returns")
                .delete()
                .eq("merchant_id", payload["merchant_id"])
                .eq("tax_year", payload["tax_year"])
                .eq("form_type", payload["form_type"])
                .execute()
            )
            result = get_supabase().table("tax_returns").insert(payload).execute()
        except Exception as exc:
            _log.error(
                "tax_returns.write_failed merchant_id=%s tax_year=%s form_type=%s",
                row.merchant_id,
                row.tax_year,
                row.form_type,
            )
            raise TaxReturnWriteError(
                f"failed to upsert tax_returns row for merchant_id={row.merchant_id} "
                f"tax_year={row.tax_year} form_type={row.form_type}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise TaxReturnWriteError(
                f"supabase insert returned no row for tax_returns "
                f"merchant_id={row.merchant_id} tax_year={row.tax_year}"
            )
        return _row_from_dict(rows[0])

    def list_for_merchant(self, merchant_id: UUID) -> list[TaxReturnRow]:
        result = (
            get_supabase()
            .table("tax_returns")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .order("tax_year", desc=True)
            .execute()
        )
        return [_row_from_dict(cast(dict[str, Any], r)) for r in (result.data or [])]


# ---------------------------------------------------------------------------
# Row encoders / decoders
# ---------------------------------------------------------------------------


def _row_to_payload(r: TaxReturnRow) -> dict[str, Any]:
    """Encode a TaxReturnRow for the Supabase wire.

    Decimal columns travel as ``str`` so Postgres ``numeric`` receives
    exact text (CLAUDE.md money rule: never a binary-float
    round-trip). Optional columns absent on the row land as ``None``
    — the migration permits NULL on every money column.
    """
    payload: dict[str, Any] = {
        "id": str(r.id),
        "merchant_id": str(r.merchant_id),
        "document_id": str(r.document_id) if r.document_id is not None else None,
        "form_type": r.form_type,
        "tax_year": r.tax_year,
        "gross_receipts": _money_to_str(r.gross_receipts),
        "net_income": _money_to_str(r.net_income),
        "total_assets": _money_to_str(r.total_assets),
        "total_liabilities": _money_to_str(r.total_liabilities),
        "officer_compensation": _money_to_str(r.officer_compensation),
        "raw_extraction": r.raw_extraction,
    }
    if r.extracted_at is not None:
        payload["extracted_at"] = r.extracted_at.isoformat()
    return payload


def _row_from_dict(d: dict[str, Any]) -> TaxReturnRow:
    """Decode a Supabase row into a TaxReturnRow.

    The reverse of ``_row_to_payload`` — strings come back from
    Postgres ``numeric`` as Python ``str`` and get re-Decimal'd; the
    null money columns stay None.
    """
    return TaxReturnRow(
        id=UUID(cast(str, d["id"])),
        merchant_id=UUID(cast(str, d["merchant_id"])),
        document_id=UUID(cast(str, d["document_id"])) if d.get("document_id") else None,
        form_type=cast(TaxFormType, d["form_type"]),
        tax_year=int(cast(int, d["tax_year"])),
        gross_receipts=_money_from_value(d.get("gross_receipts")),
        net_income=_money_from_value(d.get("net_income")),
        total_assets=_money_from_value(d.get("total_assets")),
        total_liabilities=_money_from_value(d.get("total_liabilities")),
        officer_compensation=_money_from_value(d.get("officer_compensation")),
        extracted_at=_datetime_from_value(d.get("extracted_at")),
        raw_extraction=cast(dict[str, Any] | None, d.get("raw_extraction")),
    )


def _money_to_str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _money_from_value(value: Any) -> Decimal | None:  # noqa: ANN401 — heterogeneous Supabase payload
    if value is None:
        return None
    return Decimal(str(value))


def _datetime_from_value(value: Any) -> datetime | None:  # noqa: ANN401 — heterogeneous Supabase payload
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # Supabase serializes timestamptz as ISO-8601. ``fromisoformat``
        # accepts the ``+00:00`` form Postgres emits.
        return datetime.fromisoformat(value)
    return None


__all__ = [
    "InMemoryTaxReturnRepository",
    "SupabaseTaxReturnRepository",
    "TaxReturnRepository",
    "TaxReturnRow",
    "TaxReturnWriteError",
]
