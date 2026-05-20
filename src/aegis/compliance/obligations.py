"""Compliance obligations reader (master plan §17 / §9.5).

Powers the ``/compliance/obligations`` dashboard. Reads from the
``compliance_obligations`` table created by migration 018 and produces a
typed list of obligations annotated with derived state ('overdue', 'due_soon',
'on_track') so the Jinja template doesn't need date math.

Read-only — operators write to the table via the migration's seed and via
manual `UPDATE compliance_obligations SET ...` after submitting a renewal
(the operator dashboard write-form is a future commit, not part of Phase 7).

Memory backend mirrors the Supabase one so the dashboard tests can
exercise rendering without a live DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from aegis.config import get_settings
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)

# Days inside which a "due_soon" pill renders. Anything past today is
# 'overdue'; anything beyond this horizon is 'on_track'. 60 days is the
# operator's standard prep cycle for a state filing.
_DUE_SOON_HORIZON_DAYS = 60

ObligationStatus = Literal[
    "not_started", "in_progress", "submitted", "active", "lapsed"
]
DerivedState = Literal["overdue", "due_soon", "on_track", "no_deadline"]


class ObligationRow(BaseModel):
    """One row from ``compliance_obligations`` with derived display state.

    The DB row may carry additional columns from a future migration;
    ``extra="ignore"`` keeps this readable forward-compat (same pattern
    as the audit route).
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str
    obligation_type: str
    state_code: str
    authority: str
    description: str
    deadline: date | None = None
    recurrence: str | None = None
    status: ObligationStatus
    next_due_date: date | None = None
    evidence_file_path: str | None = None
    last_reviewed: str | None = None
    notes: str | None = None

    # Derived — not a DB column.
    derived_state: DerivedState = Field(default="no_deadline")
    days_until_due: int | None = None


@dataclass(frozen=True)
class ObligationsSummary:
    """Aggregate counts for the dashboard tile."""

    total: int
    overdue: int
    due_soon: int
    on_track: int
    submitted_or_active: int


class ObligationsRepository(Protocol):
    """Backend-agnostic reader."""

    def list_obligations(self) -> list[ObligationRow]: ...


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class InMemoryObligationsRepository:
    """Test-side repository. Operator-supplied rows + the derive step."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])

    def list_obligations(self) -> list[ObligationRow]:
        return _annotate_rows(self.rows, today=date.today())


class SupabaseObligationsRepository:
    """Production reader. Reads compliance_obligations and annotates."""

    def list_obligations(self) -> list[ObligationRow]:
        try:
            result = (
                get_supabase()
                .table("compliance_obligations")
                .select("*")
                .order("next_due_date", desc=False)
                .execute()
            )
        except Exception:
            _log.warning("obligations.list_failed")
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return _annotate_rows(rows, today=date.today())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _annotate_rows(
    rows: list[dict[str, Any]], *, today: date
) -> list[ObligationRow]:
    annotated: list[ObligationRow] = []
    for raw in rows:
        try:
            row = ObligationRow.model_validate(raw)
        except Exception:
            _log.warning("obligations.bad_row id=%s", raw.get("id"))
            continue
        annotated.append(_apply_derived_state(row, today=today))
    # Sort by derived urgency: overdue -> due_soon -> on_track -> no_deadline.
    # Within a bucket, by next_due_date ascending (None last).
    order_key = {"overdue": 0, "due_soon": 1, "on_track": 2, "no_deadline": 3}
    return sorted(
        annotated,
        key=lambda r: (
            order_key[r.derived_state],
            r.next_due_date or date.max,
            r.deadline or date.max,
            r.state_code,
        ),
    )


def _apply_derived_state(
    row: ObligationRow, *, today: date
) -> ObligationRow:
    """Set derived_state + days_until_due on the row.

    Uses the earliest of next_due_date or deadline. Submitted/active
    statuses suppress overdue/due_soon (the obligation is met until the
    next recurrence rolls).
    """
    target = _earliest_deadline(row)
    if target is None:
        return row.model_copy(update={"derived_state": "no_deadline"})

    delta = (target - today).days

    if row.status in {"submitted", "active"}:
        return row.model_copy(
            update={
                "derived_state": "on_track",
                "days_until_due": delta,
            }
        )

    if delta < 0:
        derived: DerivedState = "overdue"
    elif delta <= _DUE_SOON_HORIZON_DAYS:
        derived = "due_soon"
    else:
        derived = "on_track"

    return row.model_copy(
        update={
            "derived_state": derived,
            "days_until_due": delta,
        }
    )


def _earliest_deadline(row: ObligationRow) -> date | None:
    candidates = [d for d in (row.next_due_date, row.deadline) if d is not None]
    return min(candidates) if candidates else None


def summarize(rows: list[ObligationRow]) -> ObligationsSummary:
    """Aggregate counts used by the dashboard header tiles."""
    overdue = sum(1 for r in rows if r.derived_state == "overdue")
    due_soon = sum(1 for r in rows if r.derived_state == "due_soon")
    on_track = sum(1 for r in rows if r.derived_state == "on_track")
    submitted_or_active = sum(
        1 for r in rows if r.status in {"submitted", "active"}
    )
    return ObligationsSummary(
        total=len(rows),
        overdue=overdue,
        due_soon=due_soon,
        on_track=on_track,
        submitted_or_active=submitted_or_active,
    )


def get_obligations_repository() -> ObligationsRepository:
    """Backend selector. Mirrors the rest of api.deps."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryObligationsRepository()
    return SupabaseObligationsRepository()


__all__ = [
    "DerivedState",
    "InMemoryObligationsRepository",
    "ObligationRow",
    "ObligationStatus",
    "ObligationsRepository",
    "ObligationsSummary",
    "SupabaseObligationsRepository",
    "get_obligations_repository",
    "summarize",
]
