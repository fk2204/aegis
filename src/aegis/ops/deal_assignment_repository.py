"""DealAssignmentRepository Protocol + in-memory + Supabase impls.

Backs the per-merchant assignment chip (dossier header), the "My deals"
filter on the Today dashboard, and the Assignee column on the merchant
list. Migration 076 created the underlying ``deal_assignments`` table
with a UNIQUE constraint on ``merchant_id`` — re-assignment is modeled
as DELETE + INSERT at the application layer so the audit trail captures
who was previously assigned. Un-assignment is a single DELETE.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


class DealAssignmentWriteError(RuntimeError):
    """Raised when an assignment row cannot be written."""


class DealAssignment(BaseModel):
    """Row from the ``deal_assignments`` table."""

    id: UUID = Field(default_factory=uuid4)
    merchant_id: UUID
    operator_id: UUID
    assigned_by: UUID
    assigned_at: datetime


class DealAssignmentRepository(Protocol):
    """Append-only-ish per-merchant assignment store."""

    def get_for_merchant(self, merchant_id: UUID) -> DealAssignment | None:
        """Return the current assignment row for ``merchant_id`` (or None
        when unassigned)."""

    def assign(
        self,
        *,
        merchant_id: UUID,
        operator_id: UUID,
        assigned_by: UUID,
    ) -> DealAssignment:
        """Replace any existing assignment for ``merchant_id``. The
        previous row is removed first (audit trail captures the swap
        as a remove + create at the application layer)."""

    def unassign(self, merchant_id: UUID) -> DealAssignment | None:
        """Remove the assignment for ``merchant_id``. Returns the
        previously-assigned row (so the caller can audit-attribute the
        removal) or None when the merchant was already unassigned."""

    def list_for_operator(self, operator_id: UUID) -> list[DealAssignment]:
        """Return every assignment rolling up to ``operator_id``. Used
        by the "My deals" filter on Today + merchant list."""

    def map_by_merchant(self, merchant_ids: list[UUID]) -> dict[UUID, DealAssignment]:
        """Bulk lookup: return ``{merchant_id: assignment}`` for every
        assigned merchant in the input set. Powers the Assignee column on
        the merchant list page without N+1 queries."""


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryDealAssignmentRepository:
    """Dict-backed store. Tests + offline."""

    def __init__(self) -> None:
        self._by_merchant: dict[UUID, DealAssignment] = {}

    def get_for_merchant(self, merchant_id: UUID) -> DealAssignment | None:
        return self._by_merchant.get(merchant_id)

    def assign(
        self,
        *,
        merchant_id: UUID,
        operator_id: UUID,
        assigned_by: UUID,
    ) -> DealAssignment:
        row = DealAssignment(
            merchant_id=merchant_id,
            operator_id=operator_id,
            assigned_by=assigned_by,
            assigned_at=datetime.now().astimezone(),
        )
        self._by_merchant[merchant_id] = row
        return row

    def unassign(self, merchant_id: UUID) -> DealAssignment | None:
        return self._by_merchant.pop(merchant_id, None)

    def list_for_operator(self, operator_id: UUID) -> list[DealAssignment]:
        return [row for row in self._by_merchant.values() if row.operator_id == operator_id]

    def map_by_merchant(self, merchant_ids: list[UUID]) -> dict[UUID, DealAssignment]:
        wanted = set(merchant_ids)
        return {mid: row for mid, row in self._by_merchant.items() if mid in wanted}


# ---------------------------------------------------------------------------
# Supabase backend
# ---------------------------------------------------------------------------


class SupabaseDealAssignmentRepository:
    """Reads + writes the ``deal_assignments`` table (migration 076)."""

    def get_for_merchant(self, merchant_id: UUID) -> DealAssignment | None:
        rows = (
            get_supabase()
            .table("deal_assignments")
            .select("*")
            .eq("merchant_id", str(merchant_id))
            .limit(1)
            .execute()
        )
        data = rows.data or []
        if not data:
            return None
        return _row_to_assignment(cast("dict[str, Any]", data[0]))

    def assign(
        self,
        *,
        merchant_id: UUID,
        operator_id: UUID,
        assigned_by: UUID,
    ) -> DealAssignment:
        # Remove the existing row first; the UNIQUE(merchant_id)
        # constraint would otherwise reject the INSERT.
        try:
            get_supabase().table("deal_assignments").delete().eq(
                "merchant_id", str(merchant_id)
            ).execute()
            payload: dict[str, Any] = {
                "merchant_id": str(merchant_id),
                "operator_id": str(operator_id),
                "assigned_by": str(assigned_by),
            }
            inserted = (
                get_supabase().table("deal_assignments").insert(cast("Any", payload)).execute()
            )
        except Exception as exc:
            raise DealAssignmentWriteError(
                f"failed to assign merchant {merchant_id} to operator {operator_id}"
            ) from exc
        rows = inserted.data or []
        if not rows:
            raise DealAssignmentWriteError(
                f"assign insert returned no rows for merchant {merchant_id}"
            )
        return _row_to_assignment(cast("dict[str, Any]", rows[0]))

    def unassign(self, merchant_id: UUID) -> DealAssignment | None:
        existing = self.get_for_merchant(merchant_id)
        if existing is None:
            return None
        try:
            get_supabase().table("deal_assignments").delete().eq(
                "merchant_id", str(merchant_id)
            ).execute()
        except Exception as exc:
            raise DealAssignmentWriteError(f"failed to unassign merchant {merchant_id}") from exc
        return existing

    def list_for_operator(self, operator_id: UUID) -> list[DealAssignment]:
        rows = (
            get_supabase()
            .table("deal_assignments")
            .select("*")
            .eq("operator_id", str(operator_id))
            .execute()
        )
        data = rows.data or []
        return [_row_to_assignment(cast("dict[str, Any]", r)) for r in data]

    def map_by_merchant(self, merchant_ids: list[UUID]) -> dict[UUID, DealAssignment]:
        if not merchant_ids:
            return {}
        rows = (
            get_supabase()
            .table("deal_assignments")
            .select("*")
            .in_("merchant_id", [str(m) for m in merchant_ids])
            .execute()
        )
        data = rows.data or []
        result: dict[UUID, DealAssignment] = {}
        for raw in data:
            assignment = _row_to_assignment(cast("dict[str, Any]", raw))
            result[assignment.merchant_id] = assignment
        return result


def _row_to_assignment(row: dict[str, Any]) -> DealAssignment:
    """Map a Supabase row into the DealAssignment Pydantic model."""
    return DealAssignment(
        id=UUID(str(row["id"])),
        merchant_id=UUID(str(row["merchant_id"])),
        operator_id=UUID(str(row["operator_id"])),
        assigned_by=UUID(str(row["assigned_by"])),
        assigned_at=_parse_timestamp(row["assigned_at"]),
    )


def _parse_timestamp(value: object) -> datetime:
    """Accept either a naive ISO string or an existing datetime.

    Supabase row values come back as ``object`` from the typed mapper;
    we coerce to ISO string when not already a datetime.
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


__all__ = [
    "DealAssignment",
    "DealAssignmentRepository",
    "DealAssignmentWriteError",
    "InMemoryDealAssignmentRepository",
    "SupabaseDealAssignmentRepository",
]
