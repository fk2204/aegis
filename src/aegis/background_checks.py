"""Background-checks orchestrator — fire-and-forget enqueue helper.

A merchant create event (Close webhook fresh-create branch, manual
``/ui/merchants/new``, intake form) enqueues
``run_background_checks(merchant_id)`` so the UCC + web-presence
Bedrock sweeps run off the request path. Mirrors
``aegis.close.orchestration.enqueue_close_orchestration`` in posture:
fail-soft (Redis blip audits but never raises), single audit row on
success / failure, never 5xx-es the caller.

Three call sites:

* ``_upsert_merchant_from_lead`` in ``aegis.api.routes.webhooks_close``
  on the fresh-create branch ONLY (the ``existing is None`` branch).
* ``merchant_new_submit`` in ``aegis.web.routers.merchants`` after the
  manual operator-create succeeds.
* ``intake_submit`` in ``aegis.web.routers.intake`` after the combined
  intake create succeeds.

The actual work — the Bedrock-driven UCC + web-presence sweeps and
their idempotency skip — lives in ``aegis.workers.run_background_checks``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from aegis.logger import get_logger

if TYPE_CHECKING:
    from fastapi import Request

    from aegis.audit import AuditLog

_log = get_logger(__name__)


async def enqueue_background_checks(
    *,
    request: Request,
    merchant_id: UUID,
    audit: AuditLog,
    trigger: str,
) -> bool:
    """Fire-and-forget enqueue of ``run_background_checks``.

    Returns True on success, False on enqueue failure (Redis blip etc.).
    Audits exactly one of:

      * ``merchant.background_checks.enqueued``       (success)
      * ``merchant.background_checks.enqueue_failed`` (transient enqueue
        failure — Redis disconnected, arq pool not initialized, etc.)

    Never raises. The webhook + dossier callers ignore the boolean
    return — the audit row is the durable signal.
    """
    pool: Any | None = getattr(request.app.state, "arq_pool", None)
    try:
        if pool is not None:
            await pool.enqueue_job("run_background_checks", str(merchant_id), trigger)
        else:
            pending = getattr(request.app.state, "pending_background_checks_jobs", None)
            if pending is None:
                pending = []
                request.app.state.pending_background_checks_jobs = pending
            pending.append(
                {
                    "merchant_id": str(merchant_id),
                    "trigger": trigger,
                }
            )
    except Exception as exc:
        audit.record(
            actor="system",
            action="merchant.background_checks.enqueue_failed",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "trigger": trigger,
                "error": type(exc).__name__,
                "message": str(exc)[:500],
            },
        )
        _log.warning(
            "background_checks.enqueue_failed merchant_id=%s trigger=%s error=%s",
            merchant_id,
            trigger,
            exc,
            exc_info=True,
        )
        return False

    audit.record(
        actor="system",
        action="merchant.background_checks.enqueued",
        subject_type="merchant",
        subject_id=merchant_id,
        details={"trigger": trigger},
    )
    return True


__all__ = ["enqueue_background_checks"]
