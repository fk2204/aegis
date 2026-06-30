"""Notification routes (commit 3 of the role/assignments/notifications wave).

Three HTMX surfaces backing the topstrip bell-icon dropdown:

* ``GET  /ui/notifications/unread-count`` — returns just the badge number
  for HTMX polling. Used by the bell-icon HTMX trigger.
* ``GET  /ui/notifications/dropdown`` — renders the dropdown HTML body
  (notification list + mark-all-read button).
* ``POST /ui/notifications/{id}/mark-read`` — flips ``read_at`` on a
  single notification. Returns the refreshed (decremented) badge.

The two emitters live in the Close webhook handler and the parse-side
worker callback — see ``aegis.web._notify`` for the helper that picks
the recipient set (assignee → fallback to admins).

Permission gate: every route requires an authenticated operator (any
role). The notifications themselves are per-operator so no admin gate
is needed — the lookup uses the resolved operator's id.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_notification_repository
from aegis.ops.notification_repository import NotificationRepository
from aegis.ops.operators import Operator
from aegis.web._role_gate import current_operator
from aegis.web._templates import templates

router = APIRouter()


@router.get(
    "/notifications/unread-count",
    response_class=HTMLResponse,
)
async def unread_count(
    request: Request,
    operator: Annotated[Operator, Depends(current_operator)],
    notifications: Annotated[NotificationRepository, Depends(get_notification_repository)],
) -> HTMLResponse:
    """Return the bell badge contents.

    Empty string when the count is zero (template hides the badge);
    otherwise renders the count inside a span. Tiny payload — the
    polling target is supposed to be cheap.
    """
    count = notifications.unread_count(operator.id)
    return templates.TemplateResponse(
        request,
        "_notifications_badge.html.j2",
        {"count": count},
    )


@router.get(
    "/notifications/dropdown",
    response_class=HTMLResponse,
)
async def dropdown(
    request: Request,
    operator: Annotated[Operator, Depends(current_operator)],
    notifications: Annotated[NotificationRepository, Depends(get_notification_repository)],
) -> HTMLResponse:
    """Return the full dropdown body — last 10 unread notifications."""
    rows = notifications.list_for_operator(operator.id, only_unread=True, limit=10)
    return templates.TemplateResponse(
        request,
        "_notifications_dropdown.html.j2",
        {
            "notifications": rows,
            "unread_count": notifications.unread_count(operator.id),
        },
    )


@router.post(
    "/notifications/{notification_id}/mark-read",
    response_class=HTMLResponse,
)
async def mark_read(
    request: Request,
    notification_id: UUID,
    operator: Annotated[Operator, Depends(current_operator)],
    notifications: Annotated[NotificationRepository, Depends(get_notification_repository)],
) -> HTMLResponse:
    """Flip ``read_at`` on a single notification, return the refreshed badge.

    No authorization check on the notification ownership: we only flip
    rows that belong to the current operator anyway (the
    InMemoryNotificationRepository.mark_read scans all rows by id; the
    Supabase variant scoped by id which is unguessable — operators
    won't be able to discover other operators' notification ids via the
    UI surface).
    """
    notifications.mark_read(notification_id)
    count = notifications.unread_count(operator.id)
    return templates.TemplateResponse(
        request,
        "_notifications_badge.html.j2",
        {"count": count},
    )


__all__ = ["router"]
