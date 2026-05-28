"""Shared helper for enqueuing ``process_close_attachments``.

Two call sites currently:

* ``POST /webhooks/close`` (chunk 3) — fire-and-forget after the
  merchant upsert, ``trigger='webhook'``.
* ``POST /ui/merchants/{id}/close-rescan`` (chunk 5) — operator-
  clicked manual rescan, ``trigger='rescan'``, carries the operator
  email and an optional cap-override flag.

Both must produce the same effect: enqueue on ``request.app.state.arq_pool``
when present, write a pending entry to ``app.state.pending_close_orchestration_jobs``
in tests, write a single audit row on success / failure, and never raise
back to the caller. A transient Redis blip must not 5xx an operator
request OR a Close webhook; the audit row is the durable signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from aegis.logger import get_logger

if TYPE_CHECKING:
    from fastapi import Request

    from aegis.audit import AuditLog

_log = get_logger(__name__)

_VALID_TRIGGERS: frozenset[str] = frozenset({"webhook", "rescan"})


async def enqueue_close_orchestration(
    *,
    request: Request,
    close_lead_id: str,
    merchant_id: UUID | None,
    audit: AuditLog,
    trigger: str,
    actor_email: str | None = None,
    override_cap: bool = False,
) -> bool:
    """Fire-and-forget enqueue. Returns True on success, False on failure.

    Audits exactly one of:
      * ``close.orchestration.enqueued``       (success)
      * ``close.orchestration.enqueue_failed`` (transient Redis etc.)

    Never raises. The boolean return lets the manual rescan route flash
    a user-visible message; the webhook route ignores it.
    """
    if trigger not in _VALID_TRIGGERS:
        raise ValueError(
            f"trigger must be one of {sorted(_VALID_TRIGGERS)}; got {trigger!r}"
        )

    actor = "close_webhook" if trigger == "webhook" else "dashboard"
    pool: Any | None = getattr(request.app.state, "arq_pool", None)
    try:
        if pool is not None:
            await pool.enqueue_job(
                "process_close_attachments",
                close_lead_id,
                trigger,
                actor_email=actor_email,
                override_cap=override_cap,
            )
        else:
            pending = getattr(
                request.app.state, "pending_close_orchestration_jobs", None
            )
            if pending is None:
                pending = []
                request.app.state.pending_close_orchestration_jobs = pending
            pending.append({
                "close_lead_id": close_lead_id,
                "trigger": trigger,
                "actor_email": actor_email,
                "override_cap": override_cap,
            })
    except Exception as exc:
        audit.record(
            actor=actor,
            actor_email=actor_email,
            action="close.orchestration.enqueue_failed",
            subject_type="merchant" if merchant_id is not None else None,
            subject_id=merchant_id,
            details={
                "close_lead_id": close_lead_id,
                "trigger": trigger,
                "override_cap": override_cap,
                "error": type(exc).__name__,
                "message": str(exc)[:500],
            },
        )
        _log.warning(
            "close.orchestration.enqueue_failed close_lead_id=%s trigger=%s error=%s",
            close_lead_id,
            trigger,
            exc,
            exc_info=True,
        )
        return False

    audit.record(
        actor=actor,
        actor_email=actor_email,
        action="close.orchestration.enqueued",
        subject_type="merchant" if merchant_id is not None else None,
        subject_id=merchant_id,
        details={
            "close_lead_id": close_lead_id,
            "trigger": trigger,
            "override_cap": override_cap,
        },
    )
    return True


__all__ = ["enqueue_close_orchestration"]
