"""Per-call Bedrock cost ledger (migration 078).

Dual-write companion to ``CostTrackingBedrockClient``: every Bedrock
call writes both the existing ``bedrock.usage`` audit row AND a row in
``llm_costs``. The audit_log row stays the source of truth for the
``build_weekly_digest`` aggregator; the relational table is the surface
that powers ``GET /ui/costs`` per-merchant / per-document / monthly-
trend queries without JSON probes.

Two implementations:

* :class:`InMemoryLLMCostRepository` — list-backed, used by tests and
  the in-memory storage path.
* :class:`SupabaseLLMCostRepository` — writes one row per insert to
  Postgres. Failures propagate so the wrapper can decide whether to
  swallow (it does — Bedrock cost is already incurred; the audit_log
  row stays as the canonical record).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


CallType = Literal[
    "extraction",
    "classification",
    "narrator",
    "business_intel",
    "web_presence",
    "creator_fingerprint",
]

ALL_CALL_TYPES: tuple[CallType, ...] = (
    "extraction",
    "classification",
    "narrator",
    "business_intel",
    "web_presence",
    "creator_fingerprint",
)


@dataclass(frozen=True)
class LLMCostRow:
    """One persisted Bedrock call."""

    id: UUID
    merchant_id: UUID | None
    document_id: UUID | None
    model_id: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    call_type: CallType
    called_at: datetime


@dataclass(frozen=True)
class PerMerchantCost:
    """Aggregated cost for one merchant in a window."""

    merchant_id: UUID | None
    total_cost_usd: Decimal
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    counts_by_type: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class PerDocumentCost:
    """Aggregated cost for one document in a window."""

    document_id: UUID | None
    merchant_id: UUID | None
    model_id: str
    total_cost_usd: Decimal
    call_count: int
    call_types: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MonthlyCost:
    """Total cost for one calendar month (UTC)."""

    month_iso: str  # "YYYY-MM"
    total_cost_usd: Decimal
    call_count: int


class LLMCostRepository(Protocol):
    """Persistence interface for the per-call cost ledger."""

    def insert(
        self,
        *,
        merchant_id: UUID | None,
        document_id: UUID | None,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: Decimal,
        call_type: CallType,
        called_at: datetime | None = None,
    ) -> LLMCostRow: ...

    def list_in_window(self, *, start: datetime, end: datetime) -> list[LLMCostRow]: ...

    def per_merchant(self, *, start: datetime, end: datetime) -> list[PerMerchantCost]: ...

    def per_document(self, *, start: datetime, end: datetime) -> list[PerDocumentCost]: ...

    def monthly_trend(self, *, months: int) -> list[MonthlyCost]: ...


class InMemoryLLMCostRepository:
    """List-backed implementation for tests and the in-memory storage path."""

    def __init__(self) -> None:
        self._rows: list[LLMCostRow] = []

    def insert(
        self,
        *,
        merchant_id: UUID | None,
        document_id: UUID | None,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: Decimal,
        call_type: CallType,
        called_at: datetime | None = None,
    ) -> LLMCostRow:
        from datetime import UTC

        row = LLMCostRow(
            id=uuid4(),
            merchant_id=merchant_id,
            document_id=document_id,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            call_type=call_type,
            called_at=called_at or datetime.now(UTC),
        )
        self._rows.append(row)
        return row

    def list_in_window(self, *, start: datetime, end: datetime) -> list[LLMCostRow]:
        return [r for r in self._rows if start <= r.called_at < end]

    def per_merchant(self, *, start: datetime, end: datetime) -> list[PerMerchantCost]:
        return _aggregate_per_merchant(self.list_in_window(start=start, end=end))

    def per_document(self, *, start: datetime, end: datetime) -> list[PerDocumentCost]:
        return _aggregate_per_document(self.list_in_window(start=start, end=end))

    def monthly_trend(self, *, months: int) -> list[MonthlyCost]:
        return _aggregate_monthly(self._rows, months=months)


class SupabaseLLMCostRepository:
    """Postgres-backed implementation; one INSERT per call."""

    def insert(
        self,
        *,
        merchant_id: UUID | None,
        document_id: UUID | None,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: Decimal,
        call_type: CallType,
        called_at: datetime | None = None,
    ) -> LLMCostRow:
        payload: dict[str, Any] = {
            "merchant_id": str(merchant_id) if merchant_id else None,
            "document_id": str(document_id) if document_id else None,
            "model_id": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            # Postgres numeric accepts string form; avoid float coercion.
            "estimated_cost_usd": str(estimated_cost_usd),
            "call_type": call_type,
        }
        if called_at is not None:
            payload["called_at"] = called_at.isoformat()
        result = get_supabase().table("llm_costs").insert(payload).execute()
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise RuntimeError("supabase.insert returned no row")
        return _row_to_cost(rows[0])

    def list_in_window(self, *, start: datetime, end: datetime) -> list[LLMCostRow]:
        result = (
            get_supabase()
            .table("llm_costs")
            .select("*")
            .gte("called_at", start.isoformat())
            .lt("called_at", end.isoformat())
            .order("called_at", desc=True)
            .execute()
        )
        return [_row_to_cost(cast(dict[str, Any], r)) for r in (result.data or [])]

    def per_merchant(self, *, start: datetime, end: datetime) -> list[PerMerchantCost]:
        return _aggregate_per_merchant(self.list_in_window(start=start, end=end))

    def per_document(self, *, start: datetime, end: datetime) -> list[PerDocumentCost]:
        return _aggregate_per_document(self.list_in_window(start=start, end=end))

    def monthly_trend(self, *, months: int) -> list[MonthlyCost]:
        from datetime import UTC, timedelta

        end = datetime.now(UTC)
        # months back, rounded to the 1st of the month
        first = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(months - 1):
            first = (first - timedelta(days=1)).replace(day=1)
        rows = self.list_in_window(start=first, end=end + timedelta(seconds=1))
        return _aggregate_monthly(rows, months=months)


# --- helpers --------------------------------------------------------------


def _row_to_cost(row: dict[str, Any]) -> LLMCostRow:
    return LLMCostRow(
        id=UUID(row["id"]),
        merchant_id=UUID(row["merchant_id"]) if row.get("merchant_id") else None,
        document_id=UUID(row["document_id"]) if row.get("document_id") else None,
        model_id=row["model_id"],
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        estimated_cost_usd=Decimal(str(row["estimated_cost_usd"])),
        call_type=cast(CallType, row["call_type"]),
        called_at=_parse_dt(row["called_at"]),
    )


def _parse_dt(value: Any) -> datetime:  # noqa: ANN401 — Supabase returns either str or datetime
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _aggregate_per_merchant(rows: list[LLMCostRow]) -> list[PerMerchantCost]:
    by_merchant: dict[UUID | None, list[LLMCostRow]] = defaultdict(list)
    for row in rows:
        by_merchant[row.merchant_id].append(row)
    out: list[PerMerchantCost] = []
    for merchant_id, merchant_rows in by_merchant.items():
        counts_by_type: dict[str, int] = defaultdict(int)
        for r in merchant_rows:
            counts_by_type[r.call_type] += 1
        out.append(
            PerMerchantCost(
                merchant_id=merchant_id,
                total_cost_usd=sum(
                    (r.estimated_cost_usd for r in merchant_rows), Decimal("0")
                ).quantize(Decimal("0.000001")),
                total_input_tokens=sum(r.input_tokens for r in merchant_rows),
                total_output_tokens=sum(r.output_tokens for r in merchant_rows),
                call_count=len(merchant_rows),
                counts_by_type=dict(counts_by_type),
            )
        )
    out.sort(key=lambda m: m.total_cost_usd, reverse=True)
    return out


def _aggregate_per_document(rows: list[LLMCostRow]) -> list[PerDocumentCost]:
    by_doc: dict[tuple[UUID | None, UUID | None, str], list[LLMCostRow]] = defaultdict(list)
    for row in rows:
        by_doc[(row.document_id, row.merchant_id, row.model_id)].append(row)
    out: list[PerDocumentCost] = []
    for (doc_id, merchant_id, model_id), doc_rows in by_doc.items():
        types = tuple(sorted({r.call_type for r in doc_rows}))
        out.append(
            PerDocumentCost(
                document_id=doc_id,
                merchant_id=merchant_id,
                model_id=model_id,
                total_cost_usd=sum((r.estimated_cost_usd for r in doc_rows), Decimal("0")).quantize(
                    Decimal("0.000001")
                ),
                call_count=len(doc_rows),
                call_types=types,
            )
        )
    out.sort(key=lambda d: d.total_cost_usd, reverse=True)
    return out


def _aggregate_monthly(rows: list[LLMCostRow], *, months: int) -> list[MonthlyCost]:
    by_month: dict[str, list[LLMCostRow]] = defaultdict(list)
    for row in rows:
        key = row.called_at.strftime("%Y-%m")
        by_month[key].append(row)
    out: list[MonthlyCost] = []
    for month_iso, month_rows in by_month.items():
        out.append(
            MonthlyCost(
                month_iso=month_iso,
                total_cost_usd=sum(
                    (r.estimated_cost_usd for r in month_rows), Decimal("0")
                ).quantize(Decimal("0.000001")),
                call_count=len(month_rows),
            )
        )
    out.sort(key=lambda m: m.month_iso, reverse=True)
    return out[:months]


__all__ = [
    "ALL_CALL_TYPES",
    "CallType",
    "InMemoryLLMCostRepository",
    "LLMCostRepository",
    "LLMCostRow",
    "MonthlyCost",
    "PerDocumentCost",
    "PerMerchantCost",
    "SupabaseLLMCostRepository",
]
