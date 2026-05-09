"""Append-only audit log.

Per CLAUDE.md: ``audit_log`` rows are written for every state change, not
just the high-level ones. Audit-write failures FAIL the calling operation —
never silently log-and-continue (this was a TS-version bug).

Two implementations:

  * ``InMemoryAuditLog`` — list-backed, used by tests and the in-memory
    storage path.
  * ``SupabaseAuditLog`` — writes one row per ``record()`` call to
    Postgres. A ``RuntimeError`` from Supabase propagates out so callers
    abort their operation rather than continuing with no audit trail.

The ``details`` payload is masked through ``logger._mask_value`` before it
hits the DB so PII never lands in the audit table either.
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import UUID

from aegis.db import get_supabase
from aegis.logger import _mask_value, get_logger

_log = get_logger(__name__)


class AuditWriteError(RuntimeError):
    """Raised when an audit row could not be persisted."""


class AuditLog(Protocol):
    """Append-only interface. Implementations must raise on write failure."""

    def record(
        self,
        *,
        actor: str,
        action: str,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...


class InMemoryAuditLog:
    """List-backed log. Used in tests and the in-memory storage layer."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        *,
        actor: str,
        action: str,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        masked = _mask_value(details or {})
        self.entries.append(
            {
                "actor": actor,
                "action": action,
                "subject_type": subject_type,
                "subject_id": str(subject_id) if subject_id is not None else None,
                "details": masked,
            }
        )


class SupabaseAuditLog:
    """Persists every audit row to the ``audit_log`` table.

    Insert failure raises ``AuditWriteError`` — callers must propagate so
    the originating action rolls back / fails. Never swallow.
    """

    def record(
        self,
        *,
        actor: str,
        action: str,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        masked = _mask_value(details or {})
        # JSON-serialize so non-primitive details (UUIDs, dates) hit Postgres
        # cleanly. supabase-py forwards JSONB transparently when given a dict,
        # but pre-serializing locks behavior.
        try:
            payload = {
                "actor": actor,
                "action": action,
                "subject_type": subject_type,
                "subject_id": str(subject_id) if subject_id is not None else None,
                "details": json.loads(json.dumps(masked, default=str)),
            }
            get_supabase().table("audit_log").insert(payload).execute()
        except Exception as exc:
            _log.error("audit.write_failed action=%s actor=%s", action, actor)
            raise AuditWriteError(f"failed to write audit row for {action!r}") from exc


__all__ = [
    "AuditLog",
    "AuditWriteError",
    "InMemoryAuditLog",
    "SupabaseAuditLog",
]
