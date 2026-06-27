"""NotificationRepository Protocol + in-memory + Supabase impls.

Backs the bell-icon dropdown in the topstrip. Notifications are per-
operator + mutable (``read_at`` flips false → true exactly once). Only
two event types land here today (``merchant_created`` and
``parse_complete``) — see migration 077.

The repository is thin: create, list (with an unread filter), mark
read, count unread. The bell partial in the topstrip polls
``unread_count`` every ~60s via HTMX; the dropdown lazy-loads its
items when the operator clicks the bell.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


NotificationEventType = Literal["merchant_created", "parse_complete"]


class NotificationWriteError(RuntimeError):
    """Raised when a notification row could not be written."""


class Notification(BaseModel):
    """Row from the ``notifications`` table."""

    id: UUID = Field(default_factory=uuid4)
    recipient_operator_id: UUID
    event_type: NotificationEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    link_url: str | None = None
    read_at: datetime | None = None
    created_at: datetime


class NotificationRepository(Protocol):
    """Append-mostly notification store."""

    def create(
        self,
        *,
        recipient_operator_id: UUID,
        event_type: NotificationEventType,
        payload: dict[str, Any] | None = None,
        link_url: str | None = None,
    ) -> Notification:
        """Insert a notification row and return the persisted record."""

    def list_for_operator(
        self,
        operator_id: UUID,
        *,
        only_unread: bool = True,
        limit: int = 20,
    ) -> list[Notification]:
        """Return notifications for ``operator_id``, newest first."""

    def mark_read(self, notification_id: UUID) -> None:
        """Flip ``read_at`` on a single row. Idempotent."""

    def mark_all_read(self, operator_id: UUID) -> int:
        """Mark every unread notification for ``operator_id`` as read.
        Returns the number of rows that flipped."""

    def unread_count(self, operator_id: UUID) -> int:
        """Return the count of unread notifications for ``operator_id``.
        Powers the bell badge."""


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryNotificationRepository:
    """List-backed store. Tests + offline."""

    def __init__(self) -> None:
        self._rows: list[Notification] = []

    def create(
        self,
        *,
        recipient_operator_id: UUID,
        event_type: NotificationEventType,
        payload: dict[str, Any] | None = None,
        link_url: str | None = None,
    ) -> Notification:
        row = Notification(
            recipient_operator_id=recipient_operator_id,
            event_type=event_type,
            payload=payload or {},
            link_url=link_url,
            created_at=datetime.now().astimezone(),
        )
        self._rows.append(row)
        return row

    def list_for_operator(
        self,
        operator_id: UUID,
        *,
        only_unread: bool = True,
        limit: int = 20,
    ) -> list[Notification]:
        candidates = [
            r
            for r in self._rows
            if r.recipient_operator_id == operator_id and (not only_unread or r.read_at is None)
        ]
        candidates.sort(key=lambda r: r.created_at, reverse=True)
        return candidates[: max(0, limit)]

    def mark_read(self, notification_id: UUID) -> None:
        for i, row in enumerate(self._rows):
            if row.id == notification_id and row.read_at is None:
                self._rows[i] = row.model_copy(update={"read_at": datetime.now().astimezone()})
                return

    def mark_all_read(self, operator_id: UUID) -> int:
        now = datetime.now().astimezone()
        flipped = 0
        for i, row in enumerate(self._rows):
            if row.recipient_operator_id == operator_id and row.read_at is None:
                self._rows[i] = row.model_copy(update={"read_at": now})
                flipped += 1
        return flipped

    def unread_count(self, operator_id: UUID) -> int:
        return sum(
            1 for r in self._rows if r.recipient_operator_id == operator_id and r.read_at is None
        )


# ---------------------------------------------------------------------------
# Supabase backend
# ---------------------------------------------------------------------------


class SupabaseNotificationRepository:
    """Reads + writes the ``notifications`` table (migration 077)."""

    def create(
        self,
        *,
        recipient_operator_id: UUID,
        event_type: NotificationEventType,
        payload: dict[str, Any] | None = None,
        link_url: str | None = None,
    ) -> Notification:
        body: dict[str, Any] = {
            "recipient_operator_id": str(recipient_operator_id),
            "event_type": event_type,
            "payload": payload or {},
            "link_url": link_url,
        }
        try:
            inserted = get_supabase().table("notifications").insert(cast("Any", body)).execute()
        except Exception as exc:
            raise NotificationWriteError(
                f"failed to insert notification for operator {recipient_operator_id}"
            ) from exc
        rows = inserted.data or []
        if not rows:
            raise NotificationWriteError(
                f"notification insert returned no rows for operator {recipient_operator_id}"
            )
        return _row_to_notification(cast("dict[str, Any]", rows[0]))

    def list_for_operator(
        self,
        operator_id: UUID,
        *,
        only_unread: bool = True,
        limit: int = 20,
    ) -> list[Notification]:
        query = (
            get_supabase()
            .table("notifications")
            .select("*")
            .eq("recipient_operator_id", str(operator_id))
        )
        if only_unread:
            query = query.is_("read_at", "null")
        rows = query.order("created_at", desc=True).limit(limit).execute()
        data = rows.data or []
        return [_row_to_notification(cast("dict[str, Any]", r)) for r in data]

    def mark_read(self, notification_id: UUID) -> None:
        now = datetime.now().astimezone().isoformat()
        get_supabase().table("notifications").update(cast("Any", {"read_at": now})).eq(
            "id", str(notification_id)
        ).is_("read_at", "null").execute()

    def mark_all_read(self, operator_id: UUID) -> int:
        now = datetime.now().astimezone().isoformat()
        result = (
            get_supabase()
            .table("notifications")
            .update(cast("Any", {"read_at": now}))
            .eq("recipient_operator_id", str(operator_id))
            .is_("read_at", "null")
            .execute()
        )
        return len(result.data or [])

    def unread_count(self, operator_id: UUID) -> int:
        # supabase-py's typed CountMethod literal. Use postgrest-py's
        # typed enum to avoid the str-vs-Literal mypy gap.
        from postgrest.types import CountMethod

        rows = (
            get_supabase()
            .table("notifications")
            .select("id", count=CountMethod.exact)
            .eq("recipient_operator_id", str(operator_id))
            .is_("read_at", "null")
            .execute()
        )
        return int(rows.count or 0)


def _row_to_notification(row: dict[str, Any]) -> Notification:
    """Map a Supabase row into the Notification Pydantic model."""
    raw_event = str(row["event_type"])
    if raw_event not in ("merchant_created", "parse_complete"):
        raise NotificationWriteError(f"unknown notification event_type from DB: {raw_event!r}")
    event: NotificationEventType = cast("NotificationEventType", raw_event)
    return Notification(
        id=UUID(str(row["id"])),
        recipient_operator_id=UUID(str(row["recipient_operator_id"])),
        event_type=event,
        payload=cast("dict[str, Any]", row.get("payload") or {}),
        link_url=(str(row["link_url"]) if row.get("link_url") else None),
        read_at=_parse_optional_timestamp(row.get("read_at")),
        created_at=_parse_timestamp(row["created_at"]),
    )


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value)


__all__ = [
    "InMemoryNotificationRepository",
    "Notification",
    "NotificationEventType",
    "NotificationRepository",
    "NotificationWriteError",
    "SupabaseNotificationRepository",
]
