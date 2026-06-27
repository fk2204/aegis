"""Helper for picking notification recipients + emitting rows.

Two emitters live in the rest of the codebase:

* ``aegis.api.routes.webhooks_close.process_lead_after_receipt`` — fires
  ``notify_merchant_created`` after a Close webhook lands a new lead and
  the matching merchant row is upserted.
* ``aegis.workers.parse_document`` — fires ``notify_parse_complete``
  after a parse pass completes (success OR manual_review terminal).

Recipient policy:
  * merchant_created → every active admin (no assignee exists at create
    time so we fan out so somebody sees the new deal).
  * parse_complete → the merchant's current assignee, OR every active
    admin when unassigned (don't drop the notification on the floor).

The helper logs but does NOT raise on a notification-write failure —
losing a notification row is not worth failing the parse-or-webhook job
it follows. The repository's own error wraps surface a write failure
where the caller can decide; this thin layer fails-soft on top.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from aegis.audit import AuditLog
from aegis.logger import get_logger
from aegis.ops.deal_assignment_repository import DealAssignmentRepository
from aegis.ops.notification_repository import (
    NotificationRepository,
    NotificationWriteError,
)
from aegis.ops.operator_repository import OperatorRepository

_log = get_logger(__name__)


def notify_merchant_created(
    *,
    merchant_id: UUID,
    business_name: str,
    operators: OperatorRepository,
    notifications: NotificationRepository,
    audit: AuditLog | None = None,
) -> int:
    """Fan out a ``merchant_created`` notification to every active admin.

    When ``audit`` is provided, writes one ``notification.created`` audit
    row per recipient (operator-policy directive). Returns the number of
    notification rows successfully written.
    """
    admins = operators.list_admins()
    if not admins:
        _log.warning(
            "notify.merchant_created_no_admins",
            extra={"merchant_id": str(merchant_id)},
        )
        return 0
    payload: dict[str, Any] = {
        "merchant_id": str(merchant_id),
        "summary": f"New merchant: {business_name}",
    }
    link_url = f"/ui/merchants/{merchant_id}"
    return _emit(
        notifications=notifications,
        recipients=[a.id for a in admins],
        event_type="merchant_created",
        payload=payload,
        link_url=link_url,
        audit=audit,
        subject_id=merchant_id,
    )


def notify_parse_complete(
    *,
    merchant_id: UUID,
    document_id: UUID,
    parse_status: str,
    operators: OperatorRepository,
    assignments: DealAssignmentRepository,
    notifications: NotificationRepository,
    audit: AuditLog | None = None,
) -> int:
    """Fan out a ``parse_complete`` notification.

    Recipients are the merchant's current assignee when set; otherwise
    every active admin. When ``audit`` is provided, writes one
    ``notification.created`` row per recipient. Returns the number of
    notification rows successfully written.
    """
    assignment = assignments.get_for_merchant(merchant_id)
    if assignment is not None:
        recipients: list[UUID] = [assignment.operator_id]
    else:
        recipients = [a.id for a in operators.list_admins()]
    if not recipients:
        _log.warning(
            "notify.parse_complete_no_recipients",
            extra={
                "merchant_id": str(merchant_id),
                "document_id": str(document_id),
            },
        )
        return 0
    payload: dict[str, Any] = {
        "merchant_id": str(merchant_id),
        "document_id": str(document_id),
        "parse_status": parse_status,
        "summary": f"Parse finished ({parse_status})",
    }
    link_url = f"/ui/merchants/{merchant_id}"
    return _emit(
        notifications=notifications,
        recipients=recipients,
        event_type="parse_complete",
        payload=payload,
        link_url=link_url,
        audit=audit,
        subject_id=document_id,
    )


def _emit(
    *,
    notifications: NotificationRepository,
    recipients: list[UUID],
    event_type: str,
    payload: dict[str, Any],
    link_url: str | None,
    audit: AuditLog | None = None,
    subject_id: UUID | None = None,
) -> int:
    """Best-effort fan-out helper. Counts successes; logs failures.

    ``event_type`` is narrowed by the typing.Literal at the repository
    boundary; the caller passes a string literal that satisfies it. When
    ``audit`` is provided the helper writes one ``notification.created``
    audit row per recipient (subject_type=merchant for merchant_created,
    subject_type=document for parse_complete).
    """
    written = 0
    from typing import cast

    from aegis.ops.notification_repository import NotificationEventType

    typed_event = cast("NotificationEventType", event_type)
    audit_subject_type = "merchant" if event_type == "merchant_created" else "document"
    for op_id in recipients:
        try:
            row = notifications.create(
                recipient_operator_id=op_id,
                event_type=typed_event,
                payload=payload,
                link_url=link_url,
            )
            written += 1
            if audit is not None and subject_id is not None:
                audit.record(
                    actor="notification.fanout",
                    action="notification.created",
                    subject_type=audit_subject_type,
                    subject_id=subject_id,
                    details={
                        "event_type": event_type,
                        "recipient_operator_id": str(op_id),
                        "notification_id": str(row.id),
                    },
                )
        except NotificationWriteError:
            _log.exception(
                "notify.write_failed",
                extra={"event_type": event_type, "operator_id": str(op_id)},
            )
    return written


__all__ = ["notify_merchant_created", "notify_parse_complete"]
