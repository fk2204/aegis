"""Stips repository — Supabase-backed CRUD + in-memory for tests.

Two impls (matches the funder/merchant/document pattern):
  * ``SupabaseStipRepository`` — prod
  * ``InMemoryStipRepository`` — tests

The Protocol keeps the merchants router free of Supabase imports;
tests inject the in-memory shape via dependency override.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from aegis.db import get_supabase
from aegis.stips.models import StipRow, StipStatus, StipType

_log = logging.getLogger(__name__)


class StipNotFoundError(KeyError):
    """Raised when a stip id is not found for a given merchant."""


class StipRepository(Protocol):
    def list_for_merchant(self, merchant_id: UUID) -> list[StipRow]: ...

    def get(self, merchant_id: UUID, stip_id: UUID) -> StipRow: ...

    def create(
        self,
        *,
        merchant_id: UUID,
        stip_type: StipType,
        description: str,
        funder_id: UUID | None = None,
        due_date: date | None = None,
        created_by: UUID | None = None,
        notes: str | None = None,
    ) -> StipRow: ...

    def update_status(
        self,
        *,
        merchant_id: UUID,
        stip_id: UUID,
        status: StipStatus,
        waived_reason: str | None = None,
    ) -> StipRow: ...

    def delete(self, merchant_id: UUID, stip_id: UUID) -> None: ...

    def count_outstanding(self, merchant_id: UUID) -> int: ...


# ---------------------------------------------------------------------------
# Supabase backend
# ---------------------------------------------------------------------------


class SupabaseStipRepository:
    """Reads + writes the ``stips`` table (migration 104)."""

    def list_for_merchant(self, merchant_id: UUID) -> list[StipRow]:
        result = (
            get_supabase()
            .table("stips")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .order("created_at", desc=False)
            .execute()
        )
        return [_row_to_stip(cast(dict[str, Any], r)) for r in (result.data or [])]

    def get(self, merchant_id: UUID, stip_id: UUID) -> StipRow:
        result = (
            get_supabase()
            .table("stips")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .eq("id", str(stip_id))
            .limit(1)
            .execute()
        )
        if not result.data:
            raise StipNotFoundError(str(stip_id))
        return _row_to_stip(cast(dict[str, Any], result.data[0]))

    def create(
        self,
        *,
        merchant_id: UUID,
        stip_type: StipType,
        description: str,
        funder_id: UUID | None = None,
        due_date: date | None = None,
        created_by: UUID | None = None,
        notes: str | None = None,
    ) -> StipRow:
        payload: dict[str, Any] = {
            "merchant_id": str(merchant_id),
            "stip_type": stip_type,
            "description": description,
            "status": "outstanding",
        }
        if funder_id is not None:
            payload["funder_id"] = str(funder_id)
        if due_date is not None:
            payload["due_date"] = due_date.isoformat()
        if created_by is not None:
            payload["created_by"] = str(created_by)
        if notes is not None:
            payload["notes"] = notes
        result = get_supabase().table("stips").insert(cast(Any, payload)).execute()
        if not result.data:
            raise RuntimeError("stip insert returned no rows")
        return _row_to_stip(cast(dict[str, Any], result.data[0]))

    def update_status(
        self,
        *,
        merchant_id: UUID,
        stip_id: UUID,
        status: StipStatus,
        waived_reason: str | None = None,
    ) -> StipRow:
        payload: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if status == "received":
            payload["received_at"] = datetime.now(UTC).isoformat()
        if status == "waived" and waived_reason:
            payload["waived_reason"] = waived_reason
        result = (
            get_supabase()
            .table("stips")
            .update(cast(Any, payload))
            .eq("merchant_id", str(merchant_id))
            .eq("id", str(stip_id))
            .execute()
        )
        if not result.data:
            raise StipNotFoundError(str(stip_id))
        return _row_to_stip(cast(dict[str, Any], result.data[0]))

    def delete(self, merchant_id: UUID, stip_id: UUID) -> None:
        get_supabase().table("stips").delete().eq("merchant_id", str(merchant_id)).eq(
            "id", str(stip_id)
        ).execute()

    def count_outstanding(self, merchant_id: UUID) -> int:
        from postgrest.types import CountMethod

        result = (
            get_supabase()
            .table("stips")
            .select("id", count=CountMethod.exact)
            .eq("merchant_id", str(merchant_id))
            .eq("status", "outstanding")
            .execute()
        )
        return result.count or 0


# ---------------------------------------------------------------------------
# In-memory backend (tests)
# ---------------------------------------------------------------------------


class InMemoryStipRepository:
    """Dict-backed for tests. Preserves the Supabase repo's contract."""

    def __init__(self) -> None:
        self._rows: list[StipRow] = []

    def list_for_merchant(self, merchant_id: UUID) -> list[StipRow]:
        return sorted(
            [r for r in self._rows if r.merchant_id == merchant_id],
            key=lambda r: r.created_at,
        )

    def get(self, merchant_id: UUID, stip_id: UUID) -> StipRow:
        for r in self._rows:
            if r.id == stip_id and r.merchant_id == merchant_id:
                return r
        raise StipNotFoundError(str(stip_id))

    def create(
        self,
        *,
        merchant_id: UUID,
        stip_type: StipType,
        description: str,
        funder_id: UUID | None = None,
        due_date: date | None = None,
        created_by: UUID | None = None,
        notes: str | None = None,
    ) -> StipRow:
        now = datetime.now(UTC)
        row = StipRow(
            id=uuid4(),
            merchant_id=merchant_id,
            funder_id=funder_id,
            stip_type=stip_type,
            description=description,
            status="outstanding",
            due_date=due_date,
            received_at=None,
            waived_reason=None,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            notes=notes,
        )
        self._rows.append(row)
        return row

    def update_status(
        self,
        *,
        merchant_id: UUID,
        stip_id: UUID,
        status: StipStatus,
        waived_reason: str | None = None,
    ) -> StipRow:
        for i, r in enumerate(self._rows):
            if r.id == stip_id and r.merchant_id == merchant_id:
                now = datetime.now(UTC)
                update: dict[str, Any] = {"status": status, "updated_at": now}
                if status == "received":
                    update["received_at"] = now
                if status == "waived":
                    update["waived_reason"] = waived_reason
                self._rows[i] = r.model_copy(update=update)
                return self._rows[i]
        raise StipNotFoundError(str(stip_id))

    def delete(self, merchant_id: UUID, stip_id: UUID) -> None:
        self._rows = [
            r for r in self._rows if not (r.id == stip_id and r.merchant_id == merchant_id)
        ]

    def count_outstanding(self, merchant_id: UUID) -> int:
        return sum(
            1 for r in self._rows if r.merchant_id == merchant_id and r.status == "outstanding"
        )


def _row_to_stip(row: dict[str, Any]) -> StipRow:
    """Coerce a raw Supabase row into a StipRow.

    Handles the two shapes Supabase returns: ``id`` may come back as
    a UUID string; ``due_date`` may be a plain date string; timestamps
    may be RFC 3339. Everything gets normalized to the Pydantic types
    the model declares.
    """

    def _parse_dt(v: Any) -> datetime | None:  # noqa: ANN401 — heterogeneous supabase field
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

    def _parse_date(v: Any) -> date | None:  # noqa: ANN401
        if v is None:
            return None
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        return date.fromisoformat(str(v)[:10])

    return StipRow(
        id=UUID(str(row["id"])),
        merchant_id=UUID(str(row["merchant_id"])),
        funder_id=UUID(str(row["funder_id"])) if row.get("funder_id") else None,
        stip_type=cast(StipType, row["stip_type"]),
        description=row["description"],
        status=cast(StipStatus, row.get("status") or "outstanding"),
        due_date=_parse_date(row.get("due_date")),
        received_at=_parse_dt(row.get("received_at")),
        waived_reason=row.get("waived_reason"),
        created_at=_parse_dt(row.get("created_at")) or datetime.now(UTC),
        updated_at=_parse_dt(row.get("updated_at")) or datetime.now(UTC),
        created_by=UUID(str(row["created_by"])) if row.get("created_by") else None,
        notes=row.get("notes"),
    )


__all__ = [
    "InMemoryStipRepository",
    "StipNotFoundError",
    "StipRepository",
    "SupabaseStipRepository",
]
