"""Repository round-trip + structural-guard tests for processor_statements.

Two backends, one fixture: the same ``ProcessorStatementRow`` is upserted
through ``InMemoryProcessorStatementRepository`` AND the Supabase
row-encoder / decoder helpers, and the resulting row is asserted equal
to the input. The structural-guard test enforces the
``every NEW column on migration 073 must round-trip via the Supabase
mapper too`` discipline from CLAUDE.md (the
2026-06-05 ``_row_to_document`` / ``_row_to_deal`` field-drop class of
bug — green tests against an in-memory backend that bypasses the
production mapper are worse than no tests).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.parser.processor.repository import (
    InMemoryProcessorStatementRepository,
    ProcessorStatementNotFoundError,
    ProcessorStatementRow,
    _row_from_dict,
    _row_to_payload,
)


def _row(**overrides: object) -> ProcessorStatementRow:
    """Build a minimal valid ProcessorStatementRow with sensible defaults."""
    defaults: dict[str, object] = {
        "document_id": uuid4(),
        "merchant_id": uuid4(),
        "processor_type": "stripe",
        "period_start": date(2026, 3, 1),
        "period_end": date(2026, 3, 31),
        "total_gross_volume": Decimal("12500.00"),
        "total_fees": Decimal("325.50"),
        "total_net_volume": Decimal("12174.50"),
        "total_payouts": Decimal("12174.50"),
        "avg_daily_volume": Decimal("403.23"),
        "chargeback_count": 2,
        "refund_rate": Decimal("0.0125"),
        "parse_method": "pdf_vision",
    }
    defaults.update(overrides)
    return ProcessorStatementRow(**defaults)


# ---------------------------------------------------------------------------
# InMemory implementation — basic round-trip + idempotent upsert
# ---------------------------------------------------------------------------


def test_inmemory_upsert_round_trip() -> None:
    repo = InMemoryProcessorStatementRepository()
    row = _row()
    persisted = repo.upsert(row)
    # Upsert stamps created_at when the input doesn't carry one.
    assert persisted.created_at is not None
    fetched = repo.get_by_document(row.document_id)
    assert fetched.id == row.id
    assert fetched.total_gross_volume == Decimal("12500.00")
    assert fetched.processor_type == "stripe"


def test_inmemory_get_by_document_raises_when_absent() -> None:
    repo = InMemoryProcessorStatementRepository()
    with pytest.raises(ProcessorStatementNotFoundError):
        repo.get_by_document(uuid4())


def test_inmemory_upsert_is_idempotent_on_document_id() -> None:
    """Re-upserting on the same document_id REPLACES the row.

    Mirrors the Postgres UNIQUE(document_id) + ON CONFLICT semantics
    so re-parses produce a single row, not a history."""
    repo = InMemoryProcessorStatementRepository()
    doc_id = uuid4()
    first = repo.upsert(_row(document_id=doc_id, total_gross_volume=Decimal("100.00")))
    second = repo.upsert(_row(document_id=doc_id, total_gross_volume=Decimal("250.00")))
    rows = repo.list_by_merchant(second.merchant_id)
    # second was stamped against a different merchant id (uuid4 default), so
    # filter by document_id explicitly via get_by_document.
    fetched = repo.get_by_document(doc_id)
    assert fetched.total_gross_volume == Decimal("250.00")
    # The created_at on the second upsert is preserved from the first
    # (matches Postgres DEFAULT NOW() semantics on conflict).
    assert fetched.created_at == first.created_at
    assert len(rows) >= 1


def test_inmemory_list_by_merchant_newest_first() -> None:
    repo = InMemoryProcessorStatementRepository()
    merchant_id = uuid4()
    earlier = repo.upsert(
        _row(
            merchant_id=merchant_id,
            document_id=uuid4(),
        )
    )
    # Force a younger timestamp to verify ordering.
    later = _row(merchant_id=merchant_id, document_id=uuid4())
    later = later.model_copy(update={"created_at": datetime.now(UTC)})
    repo.upsert(later)
    rows = repo.list_by_merchant(merchant_id)
    assert len(rows) == 2
    # Newest-first; later was stamped with NOW() so it should head the list.
    assert rows[0].document_id == later.document_id
    assert rows[1].document_id == earlier.document_id


def test_inmemory_list_by_merchant_empty_when_no_rows() -> None:
    repo = InMemoryProcessorStatementRepository()
    assert repo.list_by_merchant(uuid4()) == []


# ---------------------------------------------------------------------------
# Supabase mapper structural guard — CLAUDE.md "every NEW column must
# round-trip via the production mapper too"
# ---------------------------------------------------------------------------


def test_supabase_payload_contains_every_required_column() -> None:
    """The Supabase ``_row_to_payload`` encoder MUST emit every column
    the underlying table carries. Column names on the wire are the
    020-shape names (``processor``, ``gross_volume``, ...) because the
    table was created by migration 020 and only ALTERed by 073 — the
    repository encoder bridges from the dossier-shape Python field
    names to the 020-shape wire names. New columns added to migration
    073 follow-ups will fail this test until they're wired through here.
    """
    row = _row()
    payload = _row_to_payload(row)
    required = {
        # Identity + linkage.
        "id",
        "document_id",
        "merchant_id",
        # 020 column names (bridge from dossier-shape Python fields).
        "processor",
        "period_start",
        "period_end",
        "gross_volume",
        "fees_total",
        "net_revenue",
        "payouts_total",
        # New columns added by migration 073.
        "avg_daily_volume",
        "chargeback_count",
        "refund_rate",
        "parse_method",
        "raw_line_items",
        # Auditability — per-metric source-id mapping.
        "source_ids",
    }
    missing = required - set(payload.keys())
    assert not missing, f"_row_to_payload dropped required columns: {missing}"


def test_supabase_payload_money_columns_travel_as_strings() -> None:
    """CLAUDE.md money rule: Decimal → str on the wire (Postgres
    ``numeric(14,2)`` receives exact text, never a binary-float
    round-trip). The encoder MUST stringify the money columns under
    their 020-shape wire names."""
    row = _row(total_gross_volume=Decimal("12500.00"))
    payload = _row_to_payload(row)
    for key in (
        "gross_volume",
        "fees_total",
        "net_revenue",
        "payouts_total",
        "avg_daily_volume",
        "refund_rate",
    ):
        if payload[key] is not None:
            assert isinstance(payload[key], str), f"{key} must serialize as str"


def test_supabase_round_trip_preserves_every_field() -> None:
    """Encode → decode round-trip equality. Catches a mapper that
    drops or mis-types a column.
    """
    row = _row()
    payload = _row_to_payload(row)
    # The decoder reads back the row dict shape Postgres returns;
    # supplement created_at on the payload so the round-trip carries it.
    payload["created_at"] = datetime.now(UTC).isoformat()
    round_tripped = _row_from_dict(payload)
    assert round_tripped.id == row.id
    assert round_tripped.document_id == row.document_id
    assert round_tripped.merchant_id == row.merchant_id
    assert round_tripped.processor_type == row.processor_type
    assert round_tripped.period_start == row.period_start
    assert round_tripped.period_end == row.period_end
    assert round_tripped.total_gross_volume == row.total_gross_volume
    assert round_tripped.total_fees == row.total_fees
    assert round_tripped.total_net_volume == row.total_net_volume
    assert round_tripped.total_payouts == row.total_payouts
    assert round_tripped.avg_daily_volume == row.avg_daily_volume
    assert round_tripped.chargeback_count == row.chargeback_count
    assert round_tripped.refund_rate == row.refund_rate
    assert round_tripped.parse_method == row.parse_method


def test_supabase_round_trip_handles_optional_columns_as_none() -> None:
    """Period dates + refund_rate + raw_line_items are nullable; the
    mapper must accept None on both ends without raising."""
    row = _row(
        period_start=None,
        period_end=None,
        refund_rate=None,
        raw_line_items=None,
    )
    payload = _row_to_payload(row)
    assert payload["period_start"] is None
    assert payload["period_end"] is None
    assert payload["refund_rate"] is None
    assert payload["raw_line_items"] is None
    round_tripped = _row_from_dict({**payload, "created_at": datetime.now(UTC).isoformat()})
    assert round_tripped.period_start is None
    assert round_tripped.refund_rate is None
    assert round_tripped.raw_line_items is None


def test_decoder_accepts_string_money_from_postgrest() -> None:
    """PostgREST commonly returns Decimal columns as strings. The
    decoder must coerce them through ``Decimal(str(v))`` so a float
    wire value never lands as an exact-printed Decimal. Wire column
    name is the 020-shape ``gross_volume`` (bridged to the dossier-shape
    ``total_gross_volume`` Python field by the decoder)."""
    row = _row()
    payload = _row_to_payload(row)
    payload["created_at"] = datetime.now(UTC).isoformat()
    # Force the gross to a string, the way PostgREST hands them back.
    payload["gross_volume"] = "9999.99"
    round_tripped = _row_from_dict(payload)
    assert round_tripped.total_gross_volume == Decimal("9999.99")


def test_processor_type_check_constraint_enforced_by_pydantic() -> None:
    """The CHECK on the table accepts {stripe, square, toast, clover,
    paypal}. The Pydantic Literal mirrors that — an unknown brand fails
    at construction, before it reaches the database."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _row(processor_type="unknown_brand")


def test_source_ids_round_trip_preserves_per_metric_lists() -> None:
    """AEGIS auditability: every aggregate persists its contributing
    line-item IDs. The encoder serialises UUIDs as strings on the JSONB
    wire; the decoder reverses. Round-trip must preserve the per-metric
    list shape so the dossier drill-down can resolve a value back to
    the rows that produced it."""
    gross_ids = [uuid4(), uuid4(), uuid4()]
    fees_ids = [uuid4()]
    row = _row(
        source_ids={
            "gross_volume": gross_ids,
            "fees_total": fees_ids,
            "net_revenue": [],
            "payouts_total": [],
        }
    )
    payload = _row_to_payload(row)
    # On the wire the UUIDs are strings (JSON-safe).
    assert payload["source_ids"]["gross_volume"] == [str(u) for u in gross_ids]
    assert payload["source_ids"]["fees_total"] == [str(u) for u in fees_ids]
    payload["created_at"] = datetime.now(UTC).isoformat()
    round_tripped = _row_from_dict(payload)
    assert round_tripped.source_ids["gross_volume"] == gross_ids
    assert round_tripped.source_ids["fees_total"] == fees_ids
    assert round_tripped.source_ids.get("net_revenue", []) == []
