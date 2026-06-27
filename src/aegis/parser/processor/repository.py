"""ProcessorStatementRepository — Protocol + InMemory + Supabase impls.

Persistence layer for the ``processor_statements`` table introduced by
migration 020 and extended by migration 073. Mirrors the two-impl pattern
of ``aegis.funder_note_submissions.repository`` and
``aegis.pdf_store.repository``:

  * :class:`ProcessorStatementRow` is a Pydantic model carrying every
    column the dossier-shape write path populates.
  * :class:`ProcessorStatementRepository` is the Protocol the worker /
    dossier route depends on.
  * :class:`InMemoryProcessorStatementRepository` is the test / offline
    backend.
  * :class:`SupabaseProcessorStatementRepository` is the production
    backend. Decimal columns travel as ``str`` so the Postgres
    ``numeric(14,2)`` / ``numeric(6,4)`` types receive exact text —
    never a binary-float round-trip.

Column-name bridge
------------------
The Python ``ProcessorStatementRow`` carries dossier-shape field names
(``processor_type``, ``total_gross_volume``, ``total_fees``,
``total_net_volume``, ``total_payouts``). The underlying Postgres table
(migration 020) uses the validator-shape column names (``processor``,
``gross_volume``, ``fees_total``, ``net_revenue``, ``payouts_total``).
The encoder ``_row_to_payload`` translates the row fields to the 020
column names on the wire; the decoder ``_row_from_dict`` reverses. This
keeps the dossier code reading ``row.total_gross_volume`` while the
write actually lands in the existing ``gross_volume`` column — no rename
on a live table.

Why a dedicated repository (CLAUDE.md "External-integration test
discipline")
---------------------------------------------------------------------
The 2026-06-05 chunk-B / chunk-C field-drop bug happened because the
in-memory document repository bypassed the Supabase mappers and the new
columns silently never round-tripped. The repository here keeps mapper
coverage symmetrical: both backends store and return the same
``ProcessorStatementRow``, and the structural-guard test in
``tests/parser/processor/test_repository.py`` asserts that field set
end-to-end. New columns added to migration 073 follow-ups MUST extend
both backends in the same commit.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.money import Money

_log = get_logger(__name__)


ProcessorType = Literal["stripe", "square", "toast", "clover", "paypal"]


class ProcessorStatementNotFoundError(KeyError):
    """Raised when ``get_by_document`` finds no row for the document_id."""


class ProcessorStatementWriteError(RuntimeError):
    """Raised when a processor_statements row could not be persisted."""


class ProcessorStatementRow(BaseModel):
    """Application view of a row in ``processor_statements``.

    Field names carry the dossier-shape semantics (``total_gross_volume``,
    ``processor_type``, ...) — the encoder translates these to the
    underlying 020 column names (``gross_volume``, ``processor``, ...)
    on the wire. See module docstring for the column-name bridge
    rationale.

    ``source_ids`` carries the per-aggregate-metric mapping of
    contributing line-item IDs (AEGIS auditability: every aggregate
    knows which rows produced it). The dict is JSONB on the wire and
    keyed by metric name (``gross_volume``, ``fees_total``,
    ``net_revenue``, ``payouts_total``) so the dossier drill-down can
    resolve "where did this number come from?" to a specific list of
    line items.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    merchant_id: UUID
    processor_type: ProcessorType
    period_start: date | None = None
    period_end: date | None = None
    total_gross_volume: Money
    total_fees: Money
    total_net_volume: Money
    total_payouts: Money
    avg_daily_volume: Money
    chargeback_count: int = Field(default=0, ge=0)
    refund_rate: Decimal | None = Field(default=None, ge=Decimal("0"))
    parse_method: str = Field(min_length=1)
    raw_line_items: list[dict[str, Any]] | None = None
    # Per-metric source-id mapping. Empty dict on rows that pre-date the
    # source_ids capture (defensive — the column has a default of '{}'
    # on the table). Keys mirror the 020 column names so the on-disk
    # JSON shape is operator-readable.
    source_ids: dict[str, list[UUID]] = Field(default_factory=dict)
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ProcessorStatementRepository(Protocol):
    def upsert(self, row: ProcessorStatementRow) -> ProcessorStatementRow:
        """Insert or replace a row by ``document_id``.

        Idempotent: re-parses overwrite the existing row so the dossier
        always reads the latest aggregates. The UNIQUE constraint on
        ``document_id`` added by migration 073 backs the conflict
        semantics on the Supabase backend.
        """

    def get_by_document(self, document_id: UUID) -> ProcessorStatementRow:
        """Return the row for ``document_id``. Raises
        ``ProcessorStatementNotFoundError`` when absent."""

    def list_by_merchant(self, merchant_id: UUID) -> list[ProcessorStatementRow]:
        """Return every processor_statements row for ``merchant_id``,
        newest-first by ``created_at``. Empty list when the merchant
        has no processor statements on file."""


# ---------------------------------------------------------------------------
# In-memory implementation — tests + offline runs.
# ---------------------------------------------------------------------------


class InMemoryProcessorStatementRepository:
    """Dict-backed processor_statements store. Tests + offline use."""

    def __init__(self) -> None:
        # Two indexes: by row id (for completeness) and by document_id
        # (the upsert / get path). The UNIQUE constraint on document_id
        # is enforced here by replacing rather than appending.
        self._by_document: dict[UUID, ProcessorStatementRow] = {}

    def upsert(self, row: ProcessorStatementRow) -> ProcessorStatementRow:
        # Stamp created_at on the first write; preserve it on subsequent
        # overwrites so the dossier "first seen" reading is stable
        # across re-parses (matches the production Postgres DEFAULT NOW()
        # semantics: a re-upsert does not bump the timestamp unless the
        # column is set explicitly).
        if row.created_at is None:
            existing = self._by_document.get(row.document_id)
            row = row.model_copy(
                update={
                    "created_at": existing.created_at
                    if existing is not None and existing.created_at is not None
                    else datetime.now(UTC),
                }
            )
        self._by_document[row.document_id] = row
        return row

    def get_by_document(self, document_id: UUID) -> ProcessorStatementRow:
        try:
            return self._by_document[document_id]
        except KeyError as exc:
            raise ProcessorStatementNotFoundError(str(document_id)) from exc

    def list_by_merchant(self, merchant_id: UUID) -> list[ProcessorStatementRow]:
        rows = [r for r in self._by_document.values() if r.merchant_id == merchant_id]
        # Newest-first by created_at; rows without a timestamp sort to
        # the end (defensive — the upsert path always stamps one).
        rows.sort(
            key=lambda r: r.created_at or datetime.fromtimestamp(0, tz=UTC),
            reverse=True,
        )
        return rows


# ---------------------------------------------------------------------------
# Supabase implementation
# ---------------------------------------------------------------------------


class SupabaseProcessorStatementRepository:
    """Persistence backed by Postgres ``processor_statements`` (migration 020 + 073)."""

    def upsert(self, row: ProcessorStatementRow) -> ProcessorStatementRow:
        payload = _row_to_payload(row)
        try:
            result = (
                get_supabase()
                .table("processor_statements")
                .upsert(payload, on_conflict="document_id")
                .execute()
            )
        except Exception as exc:
            _log.error(
                "processor_statements.write_failed document_id=%s merchant_id=%s",
                row.document_id,
                row.merchant_id,
            )
            raise ProcessorStatementWriteError(
                f"failed to upsert processor_statements row for document_id={row.document_id}"
            ) from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise ProcessorStatementWriteError(
                f"supabase upsert returned no row for processor_statements "
                f"document_id={row.document_id}"
            )
        return _row_from_dict(rows[0])

    def get_by_document(self, document_id: UUID) -> ProcessorStatementRow:
        result = (
            get_supabase()
            .table("processor_statements")
            .select("*")
            .eq("document_id", str(document_id))
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise ProcessorStatementNotFoundError(str(document_id))
        return _row_from_dict(rows[0])

    def list_by_merchant(self, merchant_id: UUID) -> list[ProcessorStatementRow]:
        result = (
            get_supabase()
            .table("processor_statements")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .order("created_at", desc=True)
            .execute()
        )
        return [_row_from_dict(cast(dict[str, Any], r)) for r in (result.data or [])]


# ---------------------------------------------------------------------------
# Row encoders / decoders
# ---------------------------------------------------------------------------
#
# Column-name bridge: the Python row uses dossier-shape field names
# (``processor_type`` / ``total_gross_volume`` / ``total_fees`` /
# ``total_net_volume`` / ``total_payouts``) while the Postgres table
# (migration 020 + ALTERs from 073) uses validator-shape column names
# (``processor`` / ``gross_volume`` / ``fees_total`` / ``net_revenue`` /
# ``payouts_total``). The encoder maps row → wire; the decoder reverses.


def _row_to_payload(r: ProcessorStatementRow) -> dict[str, Any]:
    """Encode a ProcessorStatementRow for the Supabase wire.

    Decimal columns travel as ``str`` so Postgres ``numeric`` receives
    exact text (CLAUDE.md money rule: never a binary-float round-trip).
    Translates the dossier-shape Python field names to the 020 column
    names. Optional columns absent on the row land as ``None`` — the
    DROP NOT NULL in migration 073 permits NULL on each of them.
    """
    payload: dict[str, Any] = {
        "id": str(r.id),
        "document_id": str(r.document_id),
        "merchant_id": str(r.merchant_id),
        # Bridge: row.processor_type → wire ``processor`` (020 column).
        "processor": r.processor_type,
        "period_start": r.period_start.isoformat() if r.period_start is not None else None,
        "period_end": r.period_end.isoformat() if r.period_end is not None else None,
        # Bridge: dossier-shape field names → 020 column names.
        "gross_volume": str(r.total_gross_volume),
        "fees_total": str(r.total_fees),
        "net_revenue": str(r.total_net_volume),
        "payouts_total": str(r.total_payouts),
        # New columns added by migration 073.
        "avg_daily_volume": str(r.avg_daily_volume),
        "chargeback_count": r.chargeback_count,
        "refund_rate": str(r.refund_rate) if r.refund_rate is not None else None,
        "parse_method": r.parse_method,
        "raw_line_items": r.raw_line_items,
        # source_ids → 020's JSONB column. Encode UUIDs as strings so
        # the JSON serialiser doesn't choke; the decoder reverses.
        "source_ids": {k: [str(u) for u in v] for k, v in r.source_ids.items()},
    }
    # created_at: omit on insert so Postgres DEFAULT NOW() wins;
    # include only when the caller carries an explicit value (test
    # round-trip / replay scenarios).
    if r.created_at is not None:
        payload["created_at"] = r.created_at.isoformat()
    return payload


def _row_from_dict(row: dict[str, Any]) -> ProcessorStatementRow:
    """Decode a Postgres row dict into a ProcessorStatementRow.

    Decimal columns arrive as either strings (preferred) or floats
    (PostgREST sometimes coerces). We force-cast through ``str`` so a
    float wire value still ends up as an exact Decimal — never a binary
    representation of the printed number. Reverses the column-name
    bridge in ``_row_to_payload``.
    """

    def _dec(key: str) -> Decimal:
        v = row.get(key)
        if v is None:
            raise ProcessorStatementWriteError(
                f"processor_statements row missing required money column {key!r}"
            )
        return Decimal(str(v))

    def _dec_or_none(key: str) -> Decimal | None:
        v = row.get(key)
        return Decimal(str(v)) if v is not None else None

    def _date_or_none(key: str) -> date | None:
        v = row.get(key)
        if v is None:
            return None
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        if isinstance(v, datetime):
            return v.date()
        return date.fromisoformat(str(v))

    def _dt_or_none(key: str) -> datetime | None:
        v = row.get(key)
        if v is None:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    raw_line_items_value = row.get("raw_line_items")
    raw_line_items: list[dict[str, Any]] | None
    if raw_line_items_value is None:
        raw_line_items = None
    elif isinstance(raw_line_items_value, list):
        raw_line_items = [cast(dict[str, Any], item) for item in raw_line_items_value]
    else:
        # Defensive — JSONB normally arrives as Python list/dict via
        # supabase-py, but if PostgREST hands us a JSON-string fallback
        # we leave it unparsed rather than risk a partial decode.
        raw_line_items = None

    # source_ids: JSONB column, dict[str, list[str-uuid]]. Defensive on
    # the shape — pre-073-writer rows may carry an empty dict or null.
    raw_source_ids = row.get("source_ids") or {}
    source_ids: dict[str, list[UUID]] = {}
    if isinstance(raw_source_ids, dict):
        for key, ids in raw_source_ids.items():
            if isinstance(ids, list):
                source_ids[str(key)] = [UUID(str(u)) for u in ids if u is not None]

    return ProcessorStatementRow(
        id=UUID(str(row["id"])),
        document_id=UUID(str(row["document_id"])),
        merchant_id=UUID(str(row["merchant_id"])),
        # Bridge: wire ``processor`` (020) → row.processor_type.
        processor_type=cast(ProcessorType, str(row["processor"])),
        period_start=_date_or_none("period_start"),
        period_end=_date_or_none("period_end"),
        # Bridge: 020 column names → dossier-shape field names.
        total_gross_volume=_dec("gross_volume"),
        total_fees=_dec("fees_total"),
        total_net_volume=_dec("net_revenue"),
        total_payouts=_dec("payouts_total"),
        avg_daily_volume=_dec("avg_daily_volume"),
        chargeback_count=int(row.get("chargeback_count") or 0),
        refund_rate=_dec_or_none("refund_rate"),
        parse_method=str(row["parse_method"]),
        raw_line_items=raw_line_items,
        source_ids=source_ids,
        created_at=_dt_or_none("created_at"),
    )


__all__ = [
    "InMemoryProcessorStatementRepository",
    "ProcessorStatementNotFoundError",
    "ProcessorStatementRepository",
    "ProcessorStatementRow",
    "ProcessorStatementWriteError",
    "ProcessorType",
    "SupabaseProcessorStatementRepository",
]
