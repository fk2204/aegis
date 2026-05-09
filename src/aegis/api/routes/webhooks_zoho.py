"""POST /webhooks/zoho — receive a Zoho Deal change.

Two security layers:
  * **HMAC-SHA256 signature** in the ``X-Zoho-Webhook-Signature`` header,
    computed over the raw request body with ``ZOHO_WEBHOOK_SECRET``.
    Mismatch → 401.
  * **Timestamp freshness** — the body must include a UTC ``timestamp``
    within 5 minutes of now. Older payloads → 401 (replay protection).

Authenticated payloads are passed to ``ZohoSync.apply_inbound``, which
upserts the AEGIS merchant idempotently on ``zoho_deal_id``.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from aegis.api.deps import get_audit, get_merchant_repository
from aegis.audit import AuditLog
from aegis.config import get_settings
from aegis.merchants.repository import MerchantRepository
from aegis.zoho.sync import ZohoSync, ZohoSyncError

router = APIRouter(prefix="/webhooks/zoho", tags=["webhooks"])

WEBHOOK_FRESHNESS_SECONDS = 5 * 60


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def zoho_webhook(
    request: Request,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> None:
    body = await request.body()
    settings = get_settings()
    if settings.zoho_webhook_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ZOHO_WEBHOOK_SECRET is not configured",
        )

    secret = settings.zoho_webhook_secret.get_secret_value().encode()
    presented = request.headers.get("x-zoho-webhook-signature", "")
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad webhook signature",
        )

    # Parse JSON only after signature passes.
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid json: {exc}"
        ) from exc

    _enforce_timestamp_fresh(payload)

    deal_record = payload.get("deal") or payload.get("data") or payload
    if not isinstance(deal_record, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="webhook body missing 'deal' record",
        )

    # Build a minimal sync context: the webhook handler only needs the
    # inbound path, which doesn't require a live ZohoClient.
    from aegis.zoho.client import ZohoClient

    sync = ZohoSync(client=ZohoClient(), merchants=merchants, audit=audit)
    try:
        sync.apply_inbound(deal_record)
    except ZohoSyncError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


def _enforce_timestamp_fresh(payload: dict[str, Any]) -> None:
    ts_raw = payload.get("timestamp")
    if ts_raw is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="webhook body missing 'timestamp'",
        )
    try:
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(float(ts_raw), tz=UTC)
        else:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid timestamp: {exc}",
        ) from exc

    age = abs((datetime.now(UTC) - ts).total_seconds())
    if age > WEBHOOK_FRESHNESS_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"webhook timestamp stale ({int(age)}s)",
        )


__all__ = ["router"]
