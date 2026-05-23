"""POST /funder-replies — capture inbound funder responses (mp Phase 10).

Two entry paths share one persistence layer:

  1. Webhook (POST /funder-replies/webhook):
     HMAC-SHA256 over the raw body with
     ``FUNDER_REPLY_WEBHOOK_SECRET``, plus a body ``timestamp`` field
     within 5 minutes of now (replay protection — same shape as the
     Close webhook).

  2. Operator-paste (POST /funder-replies):
     Bearer-protected JSON endpoint. The operator pastes the email
     body + selects status + funder + deal. Useful when the funder
     replied by phone / fax / chat and there's no inbound webhook.

Both paths flow through ``aegis.funders.replies.ingest_reply`` so the
validation gate, outcome stamping, and audit semantics are identical.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit, get_funder_reply_repository
from aegis.audit import AuditLog
from aegis.config import get_settings
from aegis.funders.replies import (
    FunderReplyError,
    FunderReplyPayload,
    FunderReplyRepository,
    IngestSource,
    ReplyStatus,
    ReplyTerms,
    ingest_reply,
    parse_terms_from_json,
)
from aegis.logger import get_logger

_log = get_logger(__name__)

WEBHOOK_FRESHNESS_SECONDS = 5 * 60


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class FunderReplyRequest(BaseModel):
    """Operator-paste body shape.

    ``terms`` is optional (declined replies don't have offer terms);
    when present it accepts the same keys the LLM extractor produces
    (amount/factor/payback/term_days/daily_payment/holdback_pct).
    """

    model_config = ConfigDict(extra="forbid")

    deal_id: UUID
    funder_id: UUID
    status: ReplyStatus
    raw_text: str = Field(min_length=1)
    terms: dict[str, Any] = Field(default_factory=dict)
    parsed_confidence: int = Field(default=80, ge=0, le=100)


class FunderReplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply_id: UUID
    validation_passed: bool
    failures: list[str]
    stamped_override_id: UUID | None = None


# Single router. The bearer dep is applied per-endpoint instead of on
# the router itself because the /webhook leg authenticates via HMAC,
# not bearer — and FastAPI doesn't let us mix.

router = APIRouter(prefix="/funder-replies", tags=["funder-replies"])


@router.post(
    "",
    response_model=FunderReplyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record an operator-pasted funder reply (mp Phase 10).",
    dependencies=[Depends(require_bearer)],
)
async def operator_paste(
    body: FunderReplyRequest,
    repo: Annotated[FunderReplyRepository, Depends(get_funder_reply_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> FunderReplyResponse:
    """Operator hand-records a funder reply.

    The webhook path is preferred when the funder is set up for it;
    this endpoint covers the long tail (phone calls, ad-hoc emails,
    SMS replies).
    """
    return _ingest_via("operator_paste", body=body, repo=repo, audit=audit)


@router.post(
    "/webhook",
    response_model=FunderReplyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Inbound funder-reply webhook (HMAC-protected).",
)
async def webhook(
    request: Request,
    repo: Annotated[FunderReplyRepository, Depends(get_funder_reply_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> FunderReplyResponse:
    """Verify HMAC + freshness, then ingest via the shared path.

    Body shape matches ``FunderReplyRequest`` plus a ``timestamp``
    field (UTC ISO 8601 or epoch seconds) for replay protection.
    Returns 401 on signature or freshness failure, 503 if the
    webhook secret isn't configured.
    """
    raw = await request.body()
    settings = get_settings()
    if settings.funder_reply_webhook_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FUNDER_REPLY_WEBHOOK_SECRET is not configured",
        )

    secret = settings.funder_reply_webhook_secret.get_secret_value().encode()
    presented = request.headers.get("x-funder-webhook-signature", "")
    expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bad webhook signature",
        )

    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid json: {exc}"
        ) from exc

    _enforce_timestamp_fresh(payload)

    # Coerce to FunderReplyRequest after stripping the timestamp field
    # (it's a webhook-only concern, not part of the persisted shape).
    payload.pop("timestamp", None)
    try:
        body = FunderReplyRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid webhook body: {exc}",
        ) from exc

    return _ingest_via("webhook", body=body, repo=repo, audit=audit)


# ---------------------------------------------------------------------------
# Shared persistence path
# ---------------------------------------------------------------------------


def _ingest_via(
    ingested_via: IngestSource,
    *,
    body: FunderReplyRequest,
    repo: FunderReplyRepository,
    audit: AuditLog,
) -> FunderReplyResponse:
    try:
        terms = parse_terms_from_json(body.terms) if body.terms else ReplyTerms()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid terms: {exc}",
        ) from exc

    payload = FunderReplyPayload(
        deal_id=body.deal_id,
        funder_id=body.funder_id,
        status=body.status,
        raw_text=body.raw_text,
        ingested_via=ingested_via,
        terms=terms,
        parsed_confidence=body.parsed_confidence,
    )
    try:
        result = ingest_reply(payload, repo=repo, audit=audit)
    except FunderReplyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"funder_reply_persist_unavailable: {exc}",
        ) from exc
    return FunderReplyResponse(
        reply_id=result.reply_id,
        validation_passed=result.validation.passed,
        failures=list(result.validation.failures),
        stamped_override_id=result.stamped_override_id,
    )


def _enforce_timestamp_fresh(payload: dict[str, Any]) -> None:
    """Reject webhook payloads older than ``WEBHOOK_FRESHNESS_SECONDS``.

    Same logic as the freshness check in ``webhooks_close`` — replay
    protection. Missing timestamp → 401 so a misconfigured sender can't
    silently bypass.
    """
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
            detail=f"webhook timestamp not parseable: {exc}",
        ) from exc
    now = datetime.now(tz=UTC)
    drift = abs((now - ts).total_seconds())
    if drift > WEBHOOK_FRESHNESS_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"webhook timestamp stale: drift={int(drift)}s",
        )


__all__ = ["FunderReplyRequest", "FunderReplyResponse", "router"]
