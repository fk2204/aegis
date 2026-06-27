"""Close-webhook circuit-breaker UI surface.

Lists every lead with an open circuit (Redis counter at or above the
``OPEN_THRESHOLD``) and exposes a per-lead reset action. Operator-
gated by the existing Cloudflare Access SSO front of ``/ui/*`` — no
additional role gate today; when the Agent-4 role gate lands this
module will pick it up via the same dependency wiring used by the
other admin pages.

Two routes:

* ``GET /ui/webhooks/circuits`` — render the open-circuit table.
* ``POST /ui/webhooks/circuits/{lead_id}/reset`` — clear the counter
  for one lead and audit ``close.webhook.circuit_reset``.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from aegis.api.deps import get_audit, get_webhook_circuit
from aegis.audit import AuditLog
from aegis.logger import get_logger
from aegis.ops.webhook_circuit import (
    OPEN_THRESHOLD,
    RedisError,
    WebhookCircuit,
)
from aegis.web._templates import templates

router = APIRouter()

_log = get_logger(__name__)


@router.get("/webhooks/circuits", response_class=HTMLResponse)
async def webhook_circuits_view(
    request: Request,
    circuit: Annotated[WebhookCircuit, Depends(get_webhook_circuit)],
) -> HTMLResponse:
    """List every Close lead whose webhook circuit is currently open.

    Reads via ``WebhookCircuit.list_open_circuits``; tolerates Redis
    transport failures by surfacing an explicit error banner rather
    than 500ing the page (the operator should be able to see WHY
    they can't see the breaker state).
    """
    rows: list[tuple[str, int]]
    redis_error: str | None = None
    try:
        rows = circuit.list_open_circuits()
    except RedisError as exc:
        _log.warning("webhook_circuits_view.redis_error error=%s", exc)
        rows = []
        redis_error = str(exc)[:200]

    # Sort by count desc so the worst offenders surface first.
    rows.sort(key=lambda r: r[1], reverse=True)

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "webhook_circuits.html.j2",
            {
                "active": "Admin",
                "rows": rows,
                "threshold": OPEN_THRESHOLD,
                "redis_error": redis_error,
            },
        ),
    )


@router.post("/webhooks/circuits/{lead_id}/reset", include_in_schema=False)
async def webhook_circuits_reset(
    lead_id: str,
    circuit: Annotated[WebhookCircuit, Depends(get_webhook_circuit)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> RedirectResponse:
    """Force-clear the circuit for one lead and audit the action.

    303 redirect back to the index keeps the form-post → GET pattern
    so a browser refresh after reset doesn't repost.
    """
    circuit.reset(lead_id)
    audit.record(
        actor="operator",
        action="close.webhook.circuit_reset",
        details={"lead_id": lead_id},
    )
    return RedirectResponse(url="/ui/webhooks/circuits", status_code=303)
