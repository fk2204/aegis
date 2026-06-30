"""Close → AEGIS callback router.

A Close-side trigger (Workflow HTTP Request action, n8n step, or any
operator-controlled caller) hits ``/api/close-callback/*`` to drive
AEGIS operations without going through the operator dashboard. Four
endpoints, all parameterized by ``close_lead_id``:

  * ``GET  /api/close-callback/merchant/{close_lead_id}``     — read merchant
  * ``GET  /api/close-callback/deal/{close_lead_id}``         — read deal score
  * ``POST /api/close-callback/merchant/{close_lead_id}/upload`` — pull + parse Close attachments
  * ``POST /api/close-callback/merchant/{close_lead_id}/sync``  — push latest decision back to Close

Each endpoint:

  1. Validates ``Authorization: Bearer <CLOSE_CALLBACK_TOKEN>`` via
     ``require_close_callback_bearer``. Same pattern as ``require_bearer``
     for the operator API, but a separate env var so the Close-callback
     surface and operator API key rotate independently. Unset token →
     503 fail-closed via the boot-guard latch.
  2. Resolves ``close_lead_id`` → AEGIS ``merchant_id`` via the same
     ``find_by_close_lead_id`` lookup ``/webhooks/close`` uses.
  3. Writes an audit row with ``actor="close_callback"`` carrying the
     endpoint, ``close_lead_id``, and client IP for forensic traceability.
  4. Delegates to the same internal helpers the operator-facing surfaces
     use — no business-logic duplication. The /sync endpoint specifically
     calls ``aegis.close.sync.push_decision_to_close`` which is the single
     outbound write to ``api.close.com``, already covered by the
     ``test_outbound_hosts_restricted_to_allowlist`` invariant test.

Rate limit: 60 requests per minute per client IP via an in-process
sliding-window counter. Per-worker scope is acceptable here — the
callback router is not a high-volume surface and a runaway Close
workflow is exactly the failure mode the limit protects against.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from aegis.api.auth import require_close_callback_bearer
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_decision_snapshot,
    get_merchant_repository,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.close.client import CloseAuthError, CloseClient, CloseError
from aegis.close.orchestration import enqueue_close_orchestration
from aegis.close.sync import (
    SyncError,
    SyncResult,
    derive_ofac_status,
    push_decision_to_close,
)
from aegis.compliance.snapshot import DecisionSnapshot
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantRepository
from aegis.scoring.ofac import OFACStaleError
from aegis.storage import DocumentRepository

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — sliding-window, in-process, per client IP
# ---------------------------------------------------------------------------

_RATE_LIMIT_MAX_REQUESTS = 60
_RATE_LIMIT_WINDOW_SECONDS = 60.0


class _SlidingWindowLimiter:
    """60 req/min per IP, sliding window. Per-worker scope.

    Tradeoff: behind a multi-worker uvicorn this becomes
    ``60 * worker_count`` effective limit. The callback router runs at
    ~one request per Close event, not at sustained throughput; per-
    worker accuracy is acceptable. If we ever multi-worker AEGIS heavily
    and the limit matters, swap this for a Redis-backed limiter — the
    interface (``check(ip)``) stays the same.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, ip: str) -> None:
        now = time.monotonic()
        async with self._lock:
            stamps = self._hits.setdefault(ip, deque())
            cutoff = now - self._window_seconds
            while stamps and stamps[0] < cutoff:
                stamps.popleft()
            if len(stamps) >= self._max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Rate limit exceeded: max {self._max_requests} "
                        f"requests per {int(self._window_seconds)}s per IP"
                    ),
                )
            stamps.append(now)

    def reset(self) -> None:
        """Test helper — wipes per-IP counters between cases."""
        self._hits.clear()


_close_callback_limiter = _SlidingWindowLimiter(
    max_requests=_RATE_LIMIT_MAX_REQUESTS,
    window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
)


def reset_rate_limiter_for_tests() -> None:
    """Test-only hook — clears the rate-limit window between tests."""
    _close_callback_limiter.reset()


async def _rate_limit(request: Request) -> None:
    """Dependency: 60 req/min per client IP. Returns 429 on overflow."""
    ip = request.client.host if request.client else "unknown"
    await _close_callback_limiter.check(ip)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/close-callback",
    tags=["close-callback"],
    dependencies=[
        Depends(_rate_limit),
        Depends(require_close_callback_bearer),
    ],
)


# ---------------------------------------------------------------------------
# Response / request shapes — strict, no extras leak in.
# ---------------------------------------------------------------------------


class CallbackMerchantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: str
    business_name: str
    state: str
    industry_naics: str | None
    requested_amount: Decimal | None
    close_lead_id: str | None


class CallbackDealResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: str
    parse_status: str
    has_analysis: bool


class CallbackUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: str
    enqueued: bool


class CallbackSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: str
    patched: bool
    fields_diffed: list[str]
    reason: str


# ---------------------------------------------------------------------------
# Shared resolver
# ---------------------------------------------------------------------------


def _resolve_or_404(close_lead_id: str, merchants_repo: MerchantRepository) -> MerchantRow:
    """Look up a merchant by Close lead id; 404 if no such mapping.

    Mirrors the resolution used by ``/webhooks/close``. Generic 404
    detail — don't leak whether the lead id existed but didn't map vs
    truly doesn't exist.
    """
    merchant = merchants_repo.find_by_close_lead_id(close_lead_id)
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="merchant not found",
        )
    return merchant


def _audit_callback(
    audit: AuditLog,
    *,
    action: str,
    request: Request,
    close_lead_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Standard audit row for every close-callback request.

    ``actor`` is always ``"close_callback"`` so a grep on the audit log
    finds every Close-triggered operation regardless of endpoint. The
    raw flag string ``close_lead_id``, the matched endpoint, and the
    client IP are recorded for forensic traceability.
    """
    details: dict[str, Any] = {
        "endpoint": request.url.path,
        "close_lead_id": close_lead_id,
        "client_ip": request.client.host if request.client else "unknown",
    }
    if extra:
        details.update(extra)
    audit.record(actor="close_callback", action=action, details=details)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/merchant/{close_lead_id}",
    response_model=CallbackMerchantResponse,
    summary="Read merchant by Close lead id (Close → AEGIS).",
)
async def callback_read_merchant(
    close_lead_id: str,
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> CallbackMerchantResponse:
    """Pure read of merchant fields the Close-side workflow needs.

    No writes. No mutation of AEGIS state beyond the audit row.
    """
    merchant = _resolve_or_404(close_lead_id, merchants_repo)
    _audit_callback(
        audit,
        action="close_callback.merchant.read",
        request=request,
        close_lead_id=close_lead_id,
        extra={"merchant_id": str(merchant.id)},
    )
    return CallbackMerchantResponse(
        merchant_id=str(merchant.id),
        business_name=merchant.business_name,
        state=merchant.state,
        industry_naics=merchant.industry_naics,
        requested_amount=merchant.requested_amount,
        close_lead_id=merchant.close_lead_id,
    )


@router.get(
    "/deal/{close_lead_id}",
    response_model=CallbackDealResponse,
    summary="Read deal scoring summary by Close lead id (Close → AEGIS).",
)
async def callback_read_deal(
    close_lead_id: str,
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> CallbackDealResponse:
    """Pure read of the latest document's score + parse status."""
    merchant = _resolve_or_404(close_lead_id, merchants_repo)
    latest_docs = docs.list_documents(merchant_id=merchant.id, limit=1)
    if not latest_docs:
        _audit_callback(
            audit,
            action="close_callback.deal.read",
            request=request,
            close_lead_id=close_lead_id,
            extra={"merchant_id": str(merchant.id), "has_document": False},
        )
        return CallbackDealResponse(
            merchant_id=str(merchant.id),
            parse_status="no_document",
            has_analysis=False,
        )
    latest = latest_docs[0]
    has_analysis = docs.get_analysis(latest.id) is not None
    _audit_callback(
        audit,
        action="close_callback.deal.read",
        request=request,
        close_lead_id=close_lead_id,
        extra={
            "merchant_id": str(merchant.id),
            "document_id": str(latest.id),
            "has_document": True,
        },
    )
    return CallbackDealResponse(
        merchant_id=str(merchant.id),
        parse_status=latest.parse_status,
        has_analysis=has_analysis,
    )


@router.post(
    "/merchant/{close_lead_id}/upload",
    response_model=CallbackUploadResponse,
    summary="Trigger Close-attachment ingestion for the lead (Close → AEGIS).",
)
async def callback_trigger_upload(
    close_lead_id: str,
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> CallbackUploadResponse:
    """Enqueue the existing Close-attachment orchestration for this lead.

    Delegates to ``enqueue_close_orchestration``, the same helper the
    inbound ``/webhooks/close`` handler uses on opportunity.updated.
    The worker pulls PDFs FROM Close and runs them through the parser
    — pure inbound flow. No outbound writes to anything.
    """
    merchant = _resolve_or_404(close_lead_id, merchants_repo)
    # enqueue_close_orchestration never raises — it writes its own
    # close.orchestration.enqueued / enqueue_failed audit row and
    # returns a boolean. We add a parallel close_callback.upload.*
    # row so a grep on the audit log shows every callback-triggered
    # enqueue alongside its outcome.
    enqueued = await enqueue_close_orchestration(
        request=request,
        close_lead_id=close_lead_id,
        merchant_id=merchant.id,
        audit=audit,
        trigger="webhook",  # routed to the same worker path
    )
    _audit_callback(
        audit,
        action=(
            "close_callback.upload.enqueued" if enqueued else "close_callback.upload.enqueue_failed"
        ),
        request=request,
        close_lead_id=close_lead_id,
        extra={"merchant_id": str(merchant.id)},
    )
    if not enqueued:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="failed to enqueue Close attachment ingestion",
        )
    return CallbackUploadResponse(
        merchant_id=str(merchant.id),
        enqueued=True,
    )


@router.post(
    "/merchant/{close_lead_id}/sync",
    response_model=CallbackSyncResponse,
    summary="Push the latest stored decision to Close (Close → AEGIS).",
)
async def callback_trigger_sync(
    close_lead_id: str,
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
) -> CallbackSyncResponse:
    """Trigger ``push_decision_to_close`` for this lead.

    THIS IS THE ONLY ENDPOINT THAT WRITES OUTSIDE OF AEGIS. The write
    is bounded to ``api.close.com`` via the existing
    ``push_decision_to_close`` helper — same call graph as the
    operator-triggered ``/deals/{merchant_id}/sync-to-close`` route at
    ``src/aegis/api/routes/deals.py:335``:

        callback_trigger_sync
          └─ push_decision_to_close (aegis/close/sync.py:119)
              └─ client.get_lead(close_lead_id)          # READ Close
              └─ client.update_lead_custom_fields(...)   # WRITE Close — sole outbound mutation

    The patch body only contains ``custom.{CLOSE_FIELD_IDS[...]}`` keys
    (score, recommendation, OFAC status, applicant id, last_synced).
    Every other AEGIS outbound destination (Bedrock, Treasury OFAC,
    healthchecks, ntfy) is already in the allow-list enforced by
    ``tests/test_security_invariants.py::test_outbound_hosts_restricted_to_allowlist``.
    No funder data is read or transmitted by this path.
    """
    merchant = _resolve_or_404(close_lead_id, merchants_repo)
    if not merchant.close_lead_id:
        # Defense: ``_resolve_or_404`` found the merchant by close_lead_id
        # so this should never trigger, but the contract on
        # push_decision_to_close requires a non-empty close_lead_id.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no Close lead linkage",
        )

    deal_ids = [doc.id for doc in docs.list_documents(merchant_id=merchant.id)]
    decision = snapshot.find_latest_for_merchant(merchant.id, deal_ids=deal_ids)
    if decision is None:
        _audit_callback(
            audit,
            action="close_callback.sync.no_decision",
            request=request,
            close_lead_id=close_lead_id,
            extra={"merchant_id": str(merchant.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"merchant {merchant.id} has no recorded decision yet; "
                "score the deal first before syncing"
            ),
        )

    try:
        ofac_status = derive_ofac_status(
            decision_reason_codes=list(decision.decision_reason_codes),
            ofac_cache_timestamp=decision.ofac_cache_timestamp,
        )
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    _audit_callback(
        audit,
        action="close_callback.sync.triggered",
        request=request,
        close_lead_id=close_lead_id,
        extra={
            "merchant_id": str(merchant.id),
            "decision_id": str(decision.id),
        },
    )

    try:
        result: SyncResult = push_decision_to_close(
            close_lead_id=merchant.close_lead_id,
            decision_id=decision.id,
            score=decision.score,
            recommendation=decision.decision,
            ofac_status=ofac_status,
            client=close_client,
            audit=audit,
            now=datetime.now(tz=UTC),
            merchant=merchant,
        )
    except CloseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"close_auth_unavailable: {exc}",
        ) from exc
    except CloseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"close_upstream_error: {exc}",
        ) from exc
    except SyncError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"close_sync_unsupported_decision: {exc}",
        ) from exc

    return CallbackSyncResponse(
        merchant_id=str(merchant.id),
        patched=result.patched,
        fields_diffed=result.fields_diffed,
        reason=result.reason,
    )


__all__ = ["reset_rate_limiter_for_tests", "router"]
