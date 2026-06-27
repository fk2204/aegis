"""POST /webhooks/close — receive Close ``opportunity.updated`` events.

Two security layers (matches the pattern in ``funder_replies``):

* **HMAC-SHA256 signature** in the ``close-sig-hash`` header, computed
  over ``close-sig-timestamp + raw_body`` (concatenated, no separator)
  using ``CLOSE_WEBHOOK_SECRET`` (hex-encoded — Close returns it that
  way in the subscription POST response).
* **Timestamp freshness** — the ``close-sig-timestamp`` header must be
  within 5 minutes of now. Stale → 401 (replay protection).

Idempotency contract (mandated by commit d2ba8d5):

1. ``close.webhook.received`` audit row on EVERY reception, written
   immediately after HMAC verification passes — before any filter
   logic runs. Carries event_id, subscription_id, lead_id, opp_id,
   changed_fields, new_status_id, and a ``decision`` field
   ("processed" | "filtered_out" | "noop_idempotent").
2. Merchant upsert is keyed by ``close_lead_id`` with a read-before-write
   diff. Identical-payload redeliveries produce zero writes.
3. (Parse trigger by attachment SHA256 — implemented in step 7's hybrid
   statement path. This step records the receipt but does not enqueue
   work for attachments.)
4. (Decision push-back idempotency — owned by step 5's ``sync.py``.)

Event filtering:

* Subscribe to ``opportunity.updated`` only.
* Server-side filter narrows to status changes; the handler-side check
  re-verifies ``"status_id" in event.changed_fields`` AND
  ``event.data["status_id"] == CLOSE_DOCS_IN_PRE_UW_STATUS_ID``.
* Anything that doesn't match: audit as ``filtered_out``, return 204.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_notification_repository,
    get_operator_repository,
    get_webhook_circuit,
)
from aegis.audit import AuditLog
from aegis.background_checks import enqueue_background_checks
from aegis.close.client import CloseClient, CloseError
from aegis.close.field_map import (
    FieldMapError,
    get_custom_field,
    industry_to_naics_safe,
    normalize_entity_type_safe,
    parse_fico_range_safe,
    parse_money_safe,
    parse_product_type_safe,
    resolve_entity_type,
)
from aegis.close.orchestration import enqueue_close_orchestration
from aegis.config import Settings, get_settings
from aegis.funder_note_submissions import (
    FunderNoteSubmissionNotFoundError,
    FunderNoteSubmissionRepository,
    FunderNoteSubmissionStatus,
)
from aegis.funders.repository import FunderNotFoundError, FunderRepository
from aegis.logger import get_logger
from aegis.merchants.close_context import refresh_close_context_for_merchant
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantConflictError, MerchantRepository
from aegis.ops.notification_repository import NotificationRepository
from aegis.ops.operator_repository import OperatorRepository
from aegis.ops.webhook_circuit import WebhookCircuit
from aegis.product_types import DEFAULT_PRODUCT_TYPE
from aegis.web._notify import notify_merchant_created

router = APIRouter(prefix="/webhooks/close", tags=["webhooks"])

_log = get_logger(__name__)

WEBHOOK_FRESHNESS_SECONDS = 5 * 60


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def close_webhook(
    request: Request,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    funders: Annotated[FunderRepository, Depends(get_funder_repository)],
    circuit: Annotated[WebhookCircuit, Depends(get_webhook_circuit)],
    operators: Annotated[OperatorRepository, Depends(get_operator_repository)],
    notifications: Annotated[NotificationRepository, Depends(get_notification_repository)],
) -> None:
    raw_body = await request.body()
    client_ip = request.client.host if request.client is not None else None

    # Stage 1 — verify signature + freshness. 401 on either fail. No body
    # parsing before this point; HMAC must compute over the raw bytes.
    # Signature failures DO NOT trip the circuit — they indicate a key
    # mismatch / replay, not a payload Close keeps redelivering against a
    # lead. Lead-keyed breaker semantics depend on knowing the lead id,
    # which we don't have yet at this stage.
    _verify_signature(request.headers, raw_body, audit=audit, client_ip=client_ip)

    # Stage 2 — parse JSON. 400 on malformed. Every reject writes a
    # `close.webhook.malformed_*` audit row so a 400 flood is visible
    # to the operator without re-instrumenting in a hurry.
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        _audit_400_reject(
            audit=audit,
            reason="malformed_json",
            client_ip=client_ip,
            detail=str(exc)[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid json: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        _audit_400_reject(
            audit=audit,
            reason="payload_not_dict",
            client_ip=client_ip,
            detail=f"got {type(payload).__name__}",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="webhook body must be a JSON object",
        )

    event = payload.get("event")
    if not isinstance(event, dict):
        _audit_400_reject(
            audit=audit,
            reason="missing_event",
            client_ip=client_ip,
            detail=f"event is {type(event).__name__}",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="webhook body missing 'event' object",
        )

    # Stage 2.5 — circuit breaker check. After OPEN_THRESHOLD consecutive
    # failed processings against the same Close lead the breaker opens
    # for one hour (TTL) and short-circuits subsequent receptions with
    # 204 so Close stops retrying. The operator clears the breaker from
    # ``GET /ui/webhooks/circuits``. Audit row PER reception so the
    # frequency of suppressed retries is visible in the audit feed.
    breaker_lead_id_raw = event.get("lead_id")
    breaker_lead_id = (
        breaker_lead_id_raw
        if isinstance(breaker_lead_id_raw, str) and breaker_lead_id_raw
        else None
    )
    if breaker_lead_id is not None and circuit.is_open(breaker_lead_id):
        audit.record(
            actor="close_webhook",
            action="close.webhook.circuit_open",
            details={
                "event_id": event.get("id"),
                "subscription_id": payload.get("subscription_id"),
                "lead_id": breaker_lead_id,
                "object_type": event.get("object_type"),
                "action": event.get("action"),
            },
        )
        return None

    settings = get_settings()
    trigger_status_id = settings.close_docs_in_pre_uw_status_id

    # Stage 3 — audit reception IMMEDIATELY, before any filter or work.
    # Guarantee #1: every webhook reception leaves a durable receipt.
    matches = _matches_trigger(event, trigger_status_id)
    receipt_details = _build_receipt_details(
        event=event,
        payload=payload,
        trigger_status_id=trigger_status_id,
        matches=matches,
    )
    audit.record(
        actor="close_webhook",
        action="close.webhook.received",
        details=receipt_details,
    )

    # Wrap the remaining processing so the breaker observes the result.
    # HTTPException raised after this point counts as a failure for the
    # lead; clean completion counts as success and resets the streak.
    # Signature / JSON parse failures earlier are not lead-attributable
    # and stay outside the wrap.
    try:
        await _process_after_receipt(
            event=event,
            payload=payload,
            request=request,
            settings=settings,
            matches=matches,
            merchants=merchants,
            audit=audit,
            close_client=close_client,
            funder_note_subs=funder_note_subs,
            funders=funders,
            client_ip=client_ip,
            operators=operators,
            notifications=notifications,
        )
    except HTTPException:
        if breaker_lead_id is not None:
            circuit.record_failure(breaker_lead_id)
        raise
    if breaker_lead_id is not None:
        circuit.record_success(breaker_lead_id)
    return None


async def _process_after_receipt(
    *,
    event: dict[str, Any],
    payload: dict[str, Any],
    request: Request,
    settings: Settings,
    matches: bool,
    merchants: MerchantRepository,
    audit: AuditLog,
    close_client: CloseClient,
    funder_note_subs: FunderNoteSubmissionRepository,
    funders: FunderRepository,
    client_ip: str | None,
    operators: OperatorRepository,
    notifications: NotificationRepository,
) -> None:
    """Post-receipt processing — pulled out so the circuit-breaker
    wrapper can observe normal-vs-exceptional return cleanly.

    Body is the original handler from the lifecycle audit through the
    attachment orchestration enqueue. Behavior unchanged.
    """

    # Sprint 4 F3 — Close lifecycle audit + submission sync. Runs
    # alongside the Pre-UW trigger and is independent of ``matches``:
    # every opportunity status change leaves a ``deal.close_status_changed``
    # audit row, and Funded / Dead-Lender transitions cascade to the
    # merchant's pending funder_note_submissions. The Pre-UW trigger
    # (which also flips status_id but to the underwriting-start state)
    # gets an audit row with no submission sync because its mapping is
    # ``None`` (see ``_classify_submission_status_from_close_status``).
    _handle_lifecycle_status_change(
        event=event,
        settings=settings,
        merchants=merchants,
        funder_note_subs=funder_note_subs,
        audit=audit,
    )

    # Note-created auto-status. Independent of the Pre-UW trigger and
    # of opportunity lifecycle. Quietly no-ops unless the Close
    # subscription is configured to emit activity.note events AND the
    # note body contains a recognized decision phrase.
    _handle_note_created(
        event=event,
        merchants=merchants,
        funder_note_subs=funder_note_subs,
        funders=funders,
        close_client=close_client,
        audit=audit,
    )

    # Feature D — merchant context refresh on activity-level events.
    # Catches activity.note / activity.call / activity.email creates and
    # updates that resolve to a known merchant via ``lead_id``.
    # Independent of the Pre-UW trigger so the operator's notes / calls
    # stay live regardless of opportunity stage. Best-effort: a Close
    # API failure here MUST NOT 5xx the webhook.
    _refresh_close_context_on_activity_event(
        event=event,
        close_client=close_client,
        merchants=merchants,
        audit=audit,
    )

    if not matches:
        # Not the Pre-UW opportunity trigger. lead.updated events get a
        # parallel path so statements uploaded to a lead BEFORE the
        # opportunity reaches Pre-UW still get pulled in. SHA256 dedup
        # in process_close_attachments ensures already-processed
        # attachments are skipped, so firing this on every lead update
        # is idempotent by design.
        if _is_lead_updated(event):
            await _handle_lead_updated(
                event=event,
                request=request,
                close_client=close_client,
                merchants=merchants,
                audit=audit,
            )
        return None

    # Stage 4 — pull the Lead and upsert the merchant (idempotent).
    lead_id = event.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id:
        _audit_400_reject(
            audit=audit,
            reason="lead_id_missing_on_trigger",
            client_ip=client_ip,
            detail=f"event_id={event.get('id')!r} opp_id={event.get('object_id')!r}",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="event matches trigger filter but lead_id is missing",
        )

    # Capture the opportunity id straight off the event payload — the
    # trigger filter (`_matches_trigger`) requires
    # ``object_type == "opportunity"``, so ``object_id`` IS the
    # opportunity that fired. Persisting it on the merchant row gives
    # ``push_offer_to_opportunity`` a stable target without a follow-up
    # Close API lookup. Missing / non-string value is non-fatal:
    # webhook still upserts the merchant, the field stays NULL until
    # the next trigger.
    opp_id_raw = event.get("object_id")
    opportunity_id = opp_id_raw if isinstance(opp_id_raw, str) and opp_id_raw else None

    try:
        lead = close_client.get_lead(lead_id)
    except CloseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"close_get_lead_failed: {exc}",
        ) from exc

    fresh_merchant_created = _upsert_merchant_from_lead(
        lead=lead,
        close_lead_id=lead_id,
        close_opportunity_id=opportunity_id,
        merchants=merchants,
        audit=audit,
    )

    if fresh_merchant_created:
        # New merchant landed via this webhook — fan-out a
        # ``merchant_created`` notification to every active admin. Best-
        # effort: the helper logs (does not raise) on a write failure.
        created_merchant = merchants.find_by_close_lead_id(lead_id)
        if created_merchant is not None:
            notify_merchant_created(
                merchant_id=created_merchant.id,
                business_name=created_merchant.business_name,
                operators=operators,
                notifications=notifications,
                audit=audit,
            )

    # Feature D — merchant context refresh. After the merchant row
    # resolves we pull the Lead description + the 5 most-recent Close
    # notes + the 3 most-recent calls and persist them on the merchant
    # so the next Bedrock extraction prompt sees them.
    #
    # Best-effort: a Close API failure here MUST NOT 5xx the webhook
    # (Close retries every webhook for 72 hours; a side-path failure
    # would surface as repeated 5xxs without improving safety). We log,
    # audit the failure, and continue. The Lead payload we already
    # fetched is reused via the ``lead_fetcher`` injection so the
    # refresh costs at most 2 additional Close round-trips (note + call
    # listings).
    merchant_for_audit = merchants.find_by_close_lead_id(lead_id)
    if merchant_for_audit is not None:
        _refresh_close_context_best_effort(
            merchant_id=merchant_for_audit.id,
            lead_id=lead_id,
            lead_payload=lead,
            close_client=close_client,
            merchants=merchants,
            audit=audit,
        )

    # Stage 5 — fire-and-forget the attachment-orchestration arq job.
    # Failure to enqueue (Redis blip etc.) audits but does NOT 5xx the
    # webhook: Close will retry within 72 hours and the merchant
    # upsert is already idempotent, so a self-healing re-run is cheap.
    # Converting transient enqueue failures to 5xx would just generate
    # noise without adding any safety.
    await enqueue_close_orchestration(
        request=request,
        close_lead_id=lead_id,
        merchant_id=merchant_for_audit.id if merchant_for_audit is not None else None,
        audit=audit,
        trigger="webhook",
    )

    # Fresh merchant create only — enqueue the UCC + web-presence
    # background-checks job so the operator's first dossier render
    # already has Bedrock-derived soft signals. Updates / redeliveries
    # skip the enqueue: the dossier "Refresh" buttons remain the
    # operator-facing way to re-run on an existing merchant. Enqueue
    # failures fail-soft inside ``enqueue_background_checks`` — no 5xx.
    if fresh_merchant_created and merchant_for_audit is not None:
        await enqueue_background_checks(
            request=request,
            merchant_id=merchant_for_audit.id,
            audit=audit,
            trigger="close_webhook",
        )
    return None


# ----------------------------------------------------------------------
# Signature + freshness
# ----------------------------------------------------------------------


def _verify_signature(
    headers: Any,  # noqa: ANN401 — FastAPI Headers proxy
    raw_body: bytes,
    *,
    audit: AuditLog,
    client_ip: str | None,
) -> None:
    """Reject the request unless the close-sig-hash + close-sig-timestamp
    headers carry a valid HMAC over (timestamp + raw_body) and the
    timestamp is within the 5-minute freshness window. 401 on any fail
    with a generic body — don't leak which check failed.

    Every reject path writes a ``close.webhook.hmac_fail`` audit row +
    a ``logger.warning`` with the source IP and a non-PII reason token
    so a 400/401 flood is diagnosable from `journalctl` + `audit_log`
    without re-instrumenting in a hurry. The audit details do NOT carry
    the presented signature or the timestamp value — only the reason
    token, source IP, and the body byte length so a replay vs. drift
    pattern is visible.
    """
    settings = get_settings()
    if settings.close_webhook_secret is None:
        # Fail-closed: an unconfigured secret must not silently accept
        # signed traffic. 503 makes it clear the integration isn't
        # ready, vs 401 which would suggest a key mismatch.
        _audit_hmac_fail(
            audit=audit,
            reason="secret_unconfigured",
            client_ip=client_ip,
            body_bytes=len(raw_body),
            status_code=503,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_WEBHOOK_SECRET is not configured",
        )

    presented_sig = headers.get("close-sig-hash", "")
    timestamp_str = headers.get("close-sig-timestamp", "")
    if not presented_sig or not timestamp_str:
        _audit_hmac_fail(
            audit=audit,
            reason="missing_headers",
            client_ip=client_ip,
            body_bytes=len(raw_body),
            status_code=401,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    # Freshness — Close sends Unix epoch seconds as the timestamp; some
    # client variants emit an ISO 8601 form. Accept either.
    try:
        ts_dt = _parse_timestamp(timestamp_str)
    except ValueError:
        _audit_hmac_fail(
            audit=audit,
            reason="bad_timestamp_format",
            client_ip=client_ip,
            body_bytes=len(raw_body),
            status_code=401,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from None

    age = abs((datetime.now(UTC) - ts_dt).total_seconds())
    if age > WEBHOOK_FRESHNESS_SECONDS:
        _audit_hmac_fail(
            audit=audit,
            reason="stale_timestamp",
            client_ip=client_ip,
            body_bytes=len(raw_body),
            status_code=401,
            extra={"age_seconds": int(age)},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    # HMAC — Close gives the signature_key hex-encoded; the HMAC input
    # is `close-sig-timestamp + raw_body` (concatenated, no separator).
    secret_hex = settings.close_webhook_secret.get_secret_value()
    try:
        secret_bytes = bytes.fromhex(secret_hex)
    except ValueError:
        # Configuration error rather than auth error — the operator's
        # secret is malformed. Still don't leak it; 503 + generic detail.
        _log.error("close_webhook_secret_not_valid_hex")
        _audit_hmac_fail(
            audit=audit,
            reason="secret_not_hex",
            client_ip=client_ip,
            body_bytes=len(raw_body),
            status_code=503,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_WEBHOOK_SECRET is not valid hex",
        ) from None

    data = timestamp_str.encode("utf-8") + raw_body
    expected = hmac.new(secret_bytes, data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented_sig, expected):
        _audit_hmac_fail(
            audit=audit,
            reason="signature_mismatch",
            client_ip=client_ip,
            body_bytes=len(raw_body),
            status_code=401,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _audit_hmac_fail(
    *,
    audit: AuditLog,
    reason: str,
    client_ip: str | None,
    body_bytes: int,
    status_code: int,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write one ``close.webhook.hmac_fail`` audit row + warning log.

    Diagnostic-only: never carries PII (no body content, no signature
    bytes, no timestamp). The (reason, client_ip) pair is enough to
    tell a Close-side secret-rotation flood from an unknown-IP spoof
    attempt. Failure to write the audit row propagates per the
    ``audit_log writes are mandatory`` rule.
    """
    details: dict[str, Any] = {
        "reason": reason,
        "client_ip": client_ip,
        "body_bytes": body_bytes,
        "status_code": status_code,
    }
    if extra is not None:
        details.update(extra)
    audit.record(
        actor="close_webhook",
        action="close.webhook.hmac_fail",
        details=details,
    )
    _log.warning(
        "close_webhook.hmac_fail reason=%s client_ip=%s body_bytes=%d status=%d",
        reason,
        client_ip,
        body_bytes,
        status_code,
    )


def _audit_400_reject(
    *,
    audit: AuditLog,
    reason: str,
    client_ip: str | None,
    detail: str,
) -> None:
    """Write one ``close.webhook.bad_request`` audit row + warning log.

    Diagnostic-only — the request body has already failed shape /
    schema / trigger-precondition checks, so there is no merchant or
    lead context yet. ``detail`` is a non-PII summary (exception
    message, type name, event id) capped at 200 chars.
    """
    audit.record(
        actor="close_webhook",
        action="close.webhook.bad_request",
        details={
            "reason": reason,
            "client_ip": client_ip,
            "detail": detail[:200],
        },
    )
    _log.warning(
        "close_webhook.bad_request reason=%s client_ip=%s detail=%s",
        reason,
        client_ip,
        detail[:200],
    )


def _parse_timestamp(value: str) -> datetime:
    """Accept either Unix-epoch-seconds or ISO 8601. Raises ValueError."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty timestamp")
    try:
        epoch = int(stripped)
    except ValueError:
        pass
    else:
        return datetime.fromtimestamp(epoch, tz=UTC)
    # Fall back to ISO 8601.
    return datetime.fromisoformat(stripped.replace("Z", "+00:00"))


# ----------------------------------------------------------------------
# Event filtering
# ----------------------------------------------------------------------


def _matches_trigger(event: dict[str, Any], trigger_status_id: str) -> bool:
    """True iff this event is an Opportunity status_id change to the
    configured Docs In — Pre-UW status. Belt-and-suspenders vs the
    server-side subscription filter — both checks must pass."""
    if event.get("action") != "updated":
        return False
    if event.get("object_type") != "opportunity":
        return False
    changed_fields = event.get("changed_fields") or []
    if "status_id" not in changed_fields:
        return False
    data = event.get("data") or {}
    if data.get("status_id") != trigger_status_id:
        return False
    return True


def _is_lead_updated(event: dict[str, Any]) -> bool:
    """True iff this event is a Close lead.updated.

    Added 2026-06-18 alongside the Close webhook subscription change
    that registered ``lead.updated`` events. Lets AEGIS pull attachments
    + refresh context for leads BEFORE the opportunity transitions to
    the Pre-UW status that ``_matches_trigger`` watches.
    """
    return event.get("action") == "updated" and event.get("object_type") == "lead"


def _build_receipt_details(
    *,
    event: dict[str, Any],
    payload: dict[str, Any],
    trigger_status_id: str,
    matches: bool,
) -> dict[str, Any]:
    """Construct the structured details for the close.webhook.received
    audit row. Shape matches the idempotency contract verbatim."""
    changed_fields = list(event.get("changed_fields") or [])
    data = event.get("data") or {}
    new_status_id = data.get("status_id")

    # The receipt decision describes what THIS handler did. It does not
    # try to predict downstream noop-vs-processed at the merchant layer
    # (that's a separate signal once we have more state-tracking).
    decision = "processed" if matches else "filtered_out"

    return {
        "event_id": event.get("id"),
        "subscription_id": payload.get("subscription_id"),
        "object_type": event.get("object_type"),
        "action": event.get("action"),
        "lead_id": event.get("lead_id"),
        "opp_id": event.get("object_id"),
        "changed_fields": changed_fields,
        "new_status_id": new_status_id,
        "trigger_status_id": trigger_status_id,
        "decision": decision,
    }


# ----------------------------------------------------------------------
# Sprint 4 F3 — Close lifecycle audit + submission sync
# ----------------------------------------------------------------------


def _classify_submission_status_from_close_status(
    *,
    new_status_id: str,
    settings: Settings,
) -> FunderNoteSubmissionStatus | None:
    """Map a Close opportunity status_id to the submission status it
    should cascade to (or ``None`` for no submission update).

    The Commera Sales pipeline has every status configured as
    ``type=active`` -- there are no Close-native "won" / "lost" types
    to key off, so we match the two ID-pinned terminal statuses
    directly:

    * ``Funded``          -> ``approved``  (deal closed; pending
      submissions land on the winning track-record bucket)
    * ``Dead - Lender``   -> ``declined``  (funder said no; the
      pending submissions belong on the declined bucket)

    Every other ID (including ``Dead - Merchant`` and
    ``Dead - UW Fail``, both internal kills that don't reflect a
    funder decision) returns ``None`` -- the lifecycle audit row
    still fires but no submission rows move.
    """
    if new_status_id == settings.close_funded_status_id:
        return "approved"
    if new_status_id == settings.close_dead_lender_status_id:
        return "declined"
    return None


def _handle_lifecycle_status_change(
    *,
    event: dict[str, Any],
    settings: Settings,
    merchants: MerchantRepository,
    funder_note_subs: FunderNoteSubmissionRepository,
    audit: AuditLog,
) -> None:
    """Audit + (optionally) sync ``funder_note_submissions`` for any
    opportunity status change.

    Runs in addition to the Pre-UW trigger handling -- the two paths are
    independent and a single webhook delivery can produce a Pre-UW
    audit + a lifecycle audit in the same request when the Close
    operator transitions THROUGH the Pre-UW status.

    Effects:

    1. ``deal.close_status_changed`` audit row whenever the event is
       an opportunity.updated whose ``changed_fields`` includes
       ``status_id`` and the new id is non-empty. Always written even
       when the merchant isn't resolvable from ``close_lead_id`` (so
       the operator can investigate the orphan), and even when the
       new status doesn't trigger a submission sync.
    2. Forward-only submission sync: when the new status maps to
       ``approved`` / ``declined`` via
       ``_classify_submission_status_from_close_status`` AND the
       merchant resolves, every pending submission for the merchant
       updates to that status. ``InMemoryFunderNoteSubmissionRepository``
       and the Supabase variant both stamp ``responded_at`` on the
       first non-pending edge so the dossier history surface keeps
       the right "responded N hours after submission" reading.

    Idempotency: redeliveries are naturally safe -- the audit row
    captures ``event_id`` so dedup is downstream, and
    ``update_status`` is a no-op on already-non-pending rows because
    the only pending submissions get filtered.
    """
    if event.get("action") != "updated":
        return
    if event.get("object_type") != "opportunity":
        return
    changed_fields = event.get("changed_fields") or []
    if "status_id" not in changed_fields:
        return

    data = event.get("data") or {}
    new_status_id = data.get("status_id")
    if not isinstance(new_status_id, str) or not new_status_id:
        return

    lead_id_raw = event.get("lead_id")
    lead_id = lead_id_raw if isinstance(lead_id_raw, str) and lead_id_raw else None

    merchant: MerchantRow | None = None
    if lead_id is not None:
        try:
            merchant = merchants.find_by_close_lead_id(lead_id)
        except Exception:
            _log.warning(
                "close_webhook.lifecycle_merchant_lookup_failed lead_id=%s",
                lead_id,
                exc_info=True,
            )
            merchant = None

    new_submission_status = _classify_submission_status_from_close_status(
        new_status_id=new_status_id,
        settings=settings,
    )

    previous_data = event.get("previous_data") or {}
    previous_status_id = previous_data.get("status_id")
    if not isinstance(previous_status_id, str):
        previous_status_id = None

    synced_ids: list[str] = []
    if new_submission_status is not None and merchant is not None:
        synced_ids = _sync_pending_submissions(
            merchant_id=merchant.id,
            target_status=new_submission_status,
            funder_note_subs=funder_note_subs,
            close_lead_id=lead_id,
        )

    audit.record(
        actor="close_webhook",
        action="deal.close_status_changed",
        details={
            "event_id": event.get("id"),
            "close_lead_id": lead_id,
            "close_opportunity_id": event.get("object_id"),
            "merchant_id": str(merchant.id) if merchant is not None else None,
            "previous_status_id": previous_status_id,
            "new_status_id": new_status_id,
            "synced_submission_status": new_submission_status,
            "synced_submission_ids": synced_ids,
        },
    )


def _sync_pending_submissions(
    *,
    merchant_id: UUID,
    target_status: FunderNoteSubmissionStatus,
    funder_note_subs: FunderNoteSubmissionRepository,
    close_lead_id: str | None,
) -> list[str]:
    """Walk every pending submission for the merchant and flip it to
    ``target_status``. Returns the list of submission ids that were
    actually updated so the audit row surfaces the cascade.

    Forward-only: already-non-pending submissions are skipped, so a
    redelivery (or an operator who pre-set a row by hand) leaves the
    earlier non-pending value alone.
    """
    try:
        rows = funder_note_subs.list_for_merchant(merchant_id, limit=200)
    except Exception:
        _log.warning(
            "close_webhook.lifecycle_submissions_fetch_failed lead_id=%s",
            close_lead_id,
            exc_info=True,
        )
        return []

    updated: list[str] = []
    for row in rows:
        if row.status != "pending":
            continue
        try:
            funder_note_subs.update_status(
                row.id,
                status=target_status,
                notes="auto-synced from Close opportunity status change",
            )
        except FunderNoteSubmissionNotFoundError:
            # Concurrent delete -- treat as already-handled, log + skip.
            _log.warning(
                "close_webhook.lifecycle_submission_missing submission_id=%s",
                row.id,
            )
            continue
        updated.append(str(row.id))
    return updated


# ----------------------------------------------------------------------
# Merchant upsert
# ----------------------------------------------------------------------


def _lead_has_opportunity(close_client: CloseClient, lead_id: str) -> bool:
    """Return True when the Close lead has at least one opportunity.

    Best-effort gate for the new-merchant branch of
    ``_handle_lead_updated``. On any Close API failure we return True
    (fail-OPEN) so a transient API blip doesn't silently block a real
    deal from being captured — the original auto-create behavior is the
    safer fallback when the gate can't actually verify.
    """
    try:
        resp = close_client.request(
            "GET",
            "/api/v1/opportunity/",
            params={"lead_id": lead_id, "_limit": 1},
        )
    except CloseError:
        _log.warning(
            "close_webhook.opportunity_check_failed lead_id=%s",
            lead_id,
            exc_info=True,
        )
        return True
    data = resp.get("data")
    return isinstance(data, list) and len(data) > 0


def _lead_has_pdf_attachments(close_client: CloseClient, lead_id: str) -> bool:
    """Return True when the Close lead has at least one PDF attachment.

    Same fail-OPEN posture as ``_lead_has_opportunity`` — a Close API
    error degrades to the pre-gate auto-create behavior.
    """
    try:
        attachments = close_client.list_lead_attachments(lead_id)
    except CloseError:
        _log.warning(
            "close_webhook.attachment_check_failed lead_id=%s",
            lead_id,
            exc_info=True,
        )
        return True
    return len(attachments) > 0


async def _handle_lead_updated(
    *,
    event: dict[str, Any],
    request: Request,
    close_client: CloseClient,
    merchants: MerchantRepository,
    audit: AuditLog,
) -> None:
    """Process a ``lead.updated`` Close webhook event.

    Reuses the same merchant-upsert + context-refresh + attachment-
    orchestration helpers the Pre-UW opportunity trigger runs. The
    purpose is to catch statements uploaded to a lead BEFORE the
    opportunity moves to Pre-UW; once subscribed, every lead change
    enqueues an attachment scan whose ``process_close_attachments``
    job dedups against the existing ``documents.file_hash`` so already-
    parsed PDFs are skipped at near-zero cost.

    New-merchant gate (2026-06-20). The Close ``lead.updated``
    subscription fires for every lead change in the org — including
    leads that have no opportunity and no PDFs (cold prospects,
    duplicate leads, etc.). Before this gate, AEGIS auto-created a
    merchant row for every such event, leaving 80% of the merchants
    table empty (no docs, no scoring, just noise on the dossier).
    Two cheap Close API checks now gate the create branch:

      1. Does the lead have at least one opportunity? (``GET
         /api/v1/opportunity/?lead_id=…&_limit=1``)
      2. Does the lead have at least one PDF attachment? (existing
         ``list_lead_attachments``)

    When either check returns False, no merchant is created and an
    audit row records the skip. Both checks fail-OPEN on a Close API
    error so a transient blip degrades to the pre-gate behavior
    (auto-create) rather than silently dropping a real deal.

    The gate applies to NEW merchant creation only. When an existing
    merchant already exists for this lead the handler proceeds with
    the context refresh + attachment orchestration as before — the
    point of the gate is to stop bulk-populating empty rows, not to
    interfere with ongoing deal lifecycle.

    Failure modes match the Pre-UW path: Close ``get_lead`` failure
    becomes 503 (Close retries), ``_lead_to_merchant_fields`` parse
    error becomes 400 (operator must fix the Lead), context refresh is
    best-effort, orchestration enqueue is best-effort.
    """
    lead_id = event.get("object_id")
    if not isinstance(lead_id, str) or not lead_id:
        # Malformed event — the receipt audit row already landed in
        # ``close.webhook.received``; nothing else to do.
        return

    try:
        lead = close_client.get_lead(lead_id)
    except CloseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"close_get_lead_failed: {exc}",
        ) from exc

    existing = merchants.find_by_close_lead_id(lead_id)
    if existing is None:
        if not _lead_has_opportunity(close_client, lead_id):
            audit.record(
                actor="close_webhook",
                action="close.lead_update.skipped_no_opportunity",
                subject_type="close_lead",
                subject_id=None,
                details={"close_lead_id": lead_id},
            )
            return
        if not _lead_has_pdf_attachments(close_client, lead_id):
            audit.record(
                actor="close_webhook",
                action="close.lead_update.skipped_no_pdfs",
                subject_type="close_lead",
                subject_id=None,
                details={"close_lead_id": lead_id},
            )
            return

    fresh_merchant_created = _upsert_merchant_from_lead(
        lead=lead,
        close_lead_id=lead_id,
        # lead.updated events carry no opportunity reference. The Pre-UW
        # trigger fills ``close_opportunity_id`` later when the deal
        # actually advances.
        close_opportunity_id=None,
        merchants=merchants,
        audit=audit,
    )

    merchant = merchants.find_by_close_lead_id(lead_id)
    if merchant is None:
        # ``_upsert_merchant_from_lead`` either created the merchant or
        # raised 400 on a field-parse error. A None return here means
        # field-map raised — already surfaced as HTTPException above.
        return

    _refresh_close_context_best_effort(
        merchant_id=merchant.id,
        lead_id=lead_id,
        lead_payload=lead,
        close_client=close_client,
        merchants=merchants,
        audit=audit,
    )

    await enqueue_close_orchestration(
        request=request,
        close_lead_id=lead_id,
        merchant_id=merchant.id,
        audit=audit,
        trigger="webhook_lead_updated",
    )

    # Fresh merchant only — see the matching block in the Pre-UW
    # handler above for rationale.
    if fresh_merchant_created:
        await enqueue_background_checks(
            request=request,
            merchant_id=merchant.id,
            audit=audit,
            trigger="close_webhook_lead_updated",
        )


def _suppress_for_soft_deleted_merchant(
    *,
    close_lead_id: str,
    merchants: MerchantRepository,
    audit: AuditLog,
    race_path: bool = False,
) -> bool:
    """If a soft-deleted merchant exists for this ``close_lead_id``, write
    a ``close.webhook.suppressed_soft_deleted_merchant`` audit row and
    return ``True``. Caller short-circuits to a 204 ACK so Close stops
    retrying. Returns ``False`` when no row matches OR the matched row
    is still active.
    """
    soft_deleted = merchants.find_by_close_lead_id(close_lead_id, include_deleted=True)
    if soft_deleted is None or soft_deleted.deleted_at is None:
        return False
    details: dict[str, Any] = {
        "close_lead_id": close_lead_id,
        "deleted_at": soft_deleted.deleted_at.isoformat(),
    }
    if race_path:
        details["race_path"] = True
    audit.record(
        actor="close_webhook",
        action="close.webhook.suppressed_soft_deleted_merchant",
        subject_type="merchant",
        subject_id=soft_deleted.id,
        details=details,
    )
    return True


def _upsert_merchant_from_lead(
    *,
    lead: dict[str, Any],
    close_lead_id: str,
    close_opportunity_id: str | None,
    merchants: MerchantRepository,
    audit: AuditLog,
) -> bool:
    """Read-before-write merchant upsert keyed by close_lead_id.

    Idempotency rule (design doc guarantee #2): if a merchant row already
    exists with this close_lead_id AND none of the AEGIS-side fields
    derived from the Lead would change, do NOT write. This keeps
    redelivered events from generating noise on the merchants row's
    updated_at timestamp.

    Unknown enum values on Close-side fields (FICO bucket, Industry,
    Entity type, money strings, time-in-business cast) used to bubble
    up as ``HTTPException(400)`` and drop the entire webhook on the
    floor — every other field that DID parse went with it. The
    graceful path in ``_lead_to_merchant_fields`` now stores ``None``
    for any unparseable field and writes a
    ``close.field_parse_warning`` audit row carrying the raw value, so
    the operator can extend the static mapping tables or fix Close-side
    without losing the merchant upsert.

    ``FieldMapError`` is still raised by ``get_custom_field`` when the
    AEGIS-side name is unknown (a code bug, not Close drift), so we
    keep the catch as a backstop.

    Returns True when the call created a fresh merchant row (the caller
    uses this to enqueue background-checks ONLY on fresh creates).
    Returns False when the call was an update, a no-op idempotent
    redelivery, or a soft-delete suppression.
    """
    try:
        new_fields = _lead_to_merchant_fields(lead, close_lead_id, audit)
    except FieldMapError as exc:
        # Backstop catch — only fires when ``get_custom_field`` is called
        # with an AEGIS-side name that isn't in ``CLOSE_FIELD_IDS`` (a
        # code bug, not Close drift). Audit so the operator sees the
        # bug surface even though we're still 400ing per the original
        # intent (this branch is a defect signal, not a graceful path).
        audit.record(
            actor="close_webhook",
            action="close.webhook.field_map_backstop_failed",
            details={
                "close_lead_id": close_lead_id,
                "error": str(exc)[:200],
            },
        )
        _log.warning(
            "close_webhook.field_map_backstop_failed close_lead_id=%s error=%s",
            close_lead_id,
            str(exc)[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"close_lead_field_parse_failed: {exc}",
        ) from exc

    existing = merchants.find_by_close_lead_id(close_lead_id)

    if existing is None:
        # Soft-delete suppression: the partial unique index on
        # ``close_lead_id`` includes soft-deleted rows, so a Close
        # webhook for a lead whose merchant the operator already
        # soft-deleted would otherwise: (1) miss the active-row
        # lookup above, (2) fail the INSERT on the index, and
        # (3) leave Close storming us with retries. ACK silently
        # and respect the operator's delete intent.
        if _suppress_for_soft_deleted_merchant(
            close_lead_id=close_lead_id,
            merchants=merchants,
            audit=audit,
        ):
            return False

        new_merchant = MerchantRow(
            id=uuid4(),
            close_lead_id=close_lead_id,
            close_opportunity_id=close_opportunity_id,
            **new_fields,
        )
        try:
            merchants.upsert(new_merchant)
        except MerchantConflictError:
            # A concurrent webhook redelivery raced us between the
            # ``find_by_close_lead_id`` None-check above and our INSERT.
            # The partial unique index on ``close_lead_id`` blocked the
            # second writer; resolve by re-reading and falling through
            # to the diff-then-update branch the same way a non-racing
            # redelivery would. If the index fired against a row that's
            # not visible to the active lookup, the soft-delete
            # suppression also covers the case where an operator
            # soft-deleted the merchant between the pre-check and the
            # INSERT.
            race_winner = merchants.find_by_close_lead_id(close_lead_id)
            if race_winner is not None:
                existing = race_winner
            elif _suppress_for_soft_deleted_merchant(
                close_lead_id=close_lead_id,
                merchants=merchants,
                audit=audit,
                race_path=True,
            ):
                return False
            else:
                # Constraint fired but no row reads back, active or
                # deleted — unrecoverable.
                raise
        else:
            audit.record(
                actor="close_webhook",
                action="close.merchant.created",
                subject_type="merchant",
                subject_id=new_merchant.id,
                details={
                    "close_lead_id": close_lead_id,
                    "business_name": new_merchant.business_name,
                },
            )
            return True

    # Read-before-write diff. If nothing changed, skip the write.
    diff: dict[str, Any] = {
        key: val for key, val in new_fields.items() if getattr(existing, key, None) != val
    }
    # ``close_opportunity_id`` rides with the diff because it's
    # captured outside ``_lead_to_merchant_fields`` (it comes from the
    # event envelope, not the Lead custom fields). A first-time
    # capture or a renewal-driven new opportunity surfaces here.
    if close_opportunity_id is not None and existing.close_opportunity_id != close_opportunity_id:
        diff["close_opportunity_id"] = close_opportunity_id
    if not diff:
        # Pure idempotent redelivery. No write, no audit-row noise.
        return False

    updated = existing.model_copy(update=diff)
    merchants.upsert(updated)
    audit.record(
        actor="close_webhook",
        action="close.merchant.updated",
        subject_type="merchant",
        subject_id=existing.id,
        details={
            "close_lead_id": close_lead_id,
            "changed_keys": sorted(diff.keys()),
        },
    )
    return False


def _record_unknown_field(
    *,
    audit: AuditLog,
    field_name: str,
    raw_value: str,
    close_lead_id: str,
) -> None:
    """Write one ``close.field_parse_warning`` audit row when a Close
    payload carries a value the static mapping table doesn't recognize.

    The webhook handler keeps going with this field set to ``None`` —
    the merchant row gets created with what DID parse. The audit row
    surfaces the raw value so the operator can extend the mapping
    (FICO bucket / Industry / Entity type / etc.) or fix it in Close.

    ``raw_value`` is truncated to 200 chars so a runaway free-text
    field can't blow up the audit row.
    """
    audit.record(
        actor="close_webhook",
        action="close.field_parse_warning",
        details={
            "field": field_name,
            "raw_value": raw_value[:200],
            "close_lead_id": close_lead_id,
        },
    )


def _lead_to_merchant_fields(
    lead: dict[str, Any],
    close_lead_id: str,
    audit: AuditLog,
) -> dict[str, Any]:
    """Translate a Close Lead payload into a MerchantRow-field dict.

    All Close-specific cf_<id> lookups go through field_map.get_custom_field
    so the cf_id table stays in one place. Pure (no DB, no HTTP, modulo
    the audit-row write for unknown enum values).

    Graceful fallback policy (2026-06-26): every enum-bucket / money
    parser that COULD fail on Close drift uses the ``_safe`` variant
    that returns ``(value, warning_token)``. A ``warning_token`` lands
    in a ``close.field_parse_warning`` audit row and the field falls
    back to ``None``. The merchant upsert proceeds with whatever did
    parse — the previous behavior 400'd the entire webhook and lost
    every other field on a single bad enum.
    """
    legal_name = get_custom_field(lead, "legal_name")
    business_name = legal_name or lead.get("display_name") or lead.get("name") or ""

    state_raw = get_custom_field(lead, "state")
    # ``MerchantRow.state`` is nullable but constrained to a 2-character
    # uppercase code when set. Empty / non-string / non-2-alpha values
    # collapse to None so the webhook upsert doesn't 500 on a Pydantic
    # ``string_too_short`` validation error for leads whose Close
    # ``State`` custom field is unset. The pre-2026-06-19 path coerced
    # to "" and blew up — verified in syslog during the 2026-06-19
    # ``recover_legacy_docs --all-leads`` run when 15 newly-created
    # merchants triggered Close ``lead.updated`` webhooks against
    # leads with no operator-set state.
    state: str | None = None
    if isinstance(state_raw, str):
        candidate = state_raw.strip().upper()
        if len(candidate) == 2 and candidate.isalpha():
            state = candidate

    industry_choice = get_custom_field(lead, "industry")
    naics_explicit = get_custom_field(lead, "naics_code")
    # Prefer the operator's explicit NAICS Code when set; else derive
    # from Industry choice.
    naics: str | None
    if isinstance(naics_explicit, str) and naics_explicit.strip():
        naics = naics_explicit.strip()
    else:
        industry_input = industry_choice if isinstance(industry_choice, str) else None
        naics, naics_warning = industry_to_naics_safe(industry_input)
        if naics_warning is not None:
            _record_unknown_field(
                audit=audit,
                field_name="industry",
                raw_value=naics_warning,
                close_lead_id=close_lead_id,
            )

    raw_entity = resolve_entity_type(
        entity_type_a=get_custom_field(lead, "entity_type_a"),
        entity_type_b=get_custom_field(lead, "entity_type_b"),
        close_lead_id=close_lead_id,
        audit=audit,
    )
    entity, entity_warning = normalize_entity_type_safe(raw_entity)
    if entity_warning is not None:
        _record_unknown_field(
            audit=audit,
            field_name="entity_type",
            raw_value=entity_warning,
            close_lead_id=close_lead_id,
        )

    tib_raw = get_custom_field(lead, "time_in_business_months")
    tib_months: int | None
    if tib_raw is None or tib_raw == "":
        tib_months = None
    else:
        try:
            tib_months = int(tib_raw)
        except (ValueError, TypeError):
            tib_months = None
            _record_unknown_field(
                audit=audit,
                field_name="time_in_business_months",
                raw_value=str(tib_raw),
                close_lead_id=close_lead_id,
            )

    fico_raw = get_custom_field(lead, "fico_range")
    fico_input = fico_raw if isinstance(fico_raw, str) else None
    credit_score, fico_warning = parse_fico_range_safe(fico_input)
    if fico_warning is not None:
        _record_unknown_field(
            audit=audit,
            field_name="fico_range",
            raw_value=fico_warning,
            close_lead_id=close_lead_id,
        )

    requested_amount, money_warning = parse_money_safe(get_custom_field(lead, "requested_amount"))
    if money_warning is not None:
        _record_unknown_field(
            audit=audit,
            field_name="requested_amount",
            raw_value=money_warning,
            close_lead_id=close_lead_id,
        )

    # Migration 080 — Product Type. The Close custom-field cf_id may not
    # be registered in ``CLOSE_FIELD_IDS`` yet (operator hasn't created
    # the field in the Commera Close account at migration time). The
    # strict ``get_custom_field`` raises ``FieldMapError`` on an
    # unregistered AEGIS-side name; catch and treat as "field not yet
    # available" so the merchant upsert proceeds with the project default
    # (revenue_based — Commera's pre-080 universal value). Once the
    # operator adds the cf_id, the strict path lights up automatically
    # and unknown CHOICE values land in a close.field_parse_warning
    # audit row just like the other ``_safe`` parsers above.
    raw_product: object | None
    try:
        raw_product = get_custom_field(lead, "product_type")
    except FieldMapError:
        raw_product = None
    product_value, product_warning = parse_product_type_safe(raw_product)
    if product_warning is not None:
        _record_unknown_field(
            audit=audit,
            field_name="product_type",
            raw_value=product_warning,
            close_lead_id=close_lead_id,
        )
    product_type_value = product_value or DEFAULT_PRODUCT_TYPE

    return {
        "business_name": str(business_name),
        "dba": _str_or_none(get_custom_field(lead, "dba_name")),
        "ein": _str_or_none(get_custom_field(lead, "ein")),
        "owner_name": _str_or_none(get_custom_field(lead, "owner_name")),
        "state": state,
        "industry_naics": naics,
        # Persist the raw Lead-side Industry choice string alongside
        # the derived NAICS. Drives ``aegis.scoring_v2.industry`` tier
        # lookup on the dossier + Track B band adjustment. Migration 055.
        "industry_choice": (
            industry_choice if isinstance(industry_choice, str) and industry_choice else None
        ),
        "time_in_business_months": tib_months,
        "credit_score": credit_score,
        "requested_amount": requested_amount,
        "entity_type": entity,
        # Migration 080 — product type at merchant create time. Drives
        # offer sizing, narrator framing, and funder matching.
        "product_type": product_type_value,
    }


def _str_or_none(value: Any) -> str | None:  # noqa: ANN401 — Close custom-field values are heterogeneous
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ----------------------------------------------------------------------
# Note-driven auto-status (Sprint 7, 2026-06-17)
# ----------------------------------------------------------------------
#
# Operators record funder responses in Close as plain Notes:
#
#   "Kapitus approved for $45k at 1.35"
#   "DECLINED by Velocity — too many positions"
#   "Counter offer 50k"
#
# This handler watches the activity.note stream and flips the matching
# pending submission row to the right status. Requires the Close
# webhook subscription to include ``activity.note`` events — until the
# operator extends the subscription this code is dormant.
#
# Safety properties:
#   * Idempotent per Close activity id (audit details.activity_id).
#   * Skips silently when there are zero pending submissions.
#   * Skips with `submission.auto_status_ambiguous` audit row when
#     there is more than one pending submission — the matcher is not
#     authoritative about which funder a free-text note refers to.
#   * Pattern matches use word boundaries — "Their CPA approved the
#     financials" requires a more specific phrase to fire.
#   * On approved transitions, posts a "Fund the deal" Close task.


_NoteDecisionStatus = Literal["approved", "declined", "countered"]

# Order matters: declined / countered checked BEFORE approved so that
# negation phrases like "not approved" reach the declined pattern first.
# Re-IGNORECASE makes "APPROVED" and "approved" equivalent at the regex
# level, so a bare positional match on the approved pattern would
# otherwise swallow "not approved" before we reach the declined one.
_NOTE_STATUS_PATTERNS: dict[_NoteDecisionStatus, re.Pattern[str]] = {
    "declined": re.compile(
        r"\b(?:DECLINED|declined\s+by|not\s+approved|decline[d]?\b\s+(?:by|—|-))\b",
        re.IGNORECASE,
    ),
    "countered": re.compile(
        r"\b(?:COUNTERED|counter\s+offer|counter\s+at|counter\s+of)\b",
        re.IGNORECASE,
    ),
    "approved": re.compile(
        r"\b(?:APPROVED|approved\s+for|they\s+approved|approved\s+by|approved\s+at)\b",
        re.IGNORECASE,
    ),
}

# Money pattern. Matches `$45,000`, `45000.00`, `$45k`, `45k`, `1.5M`.
# Capture: (digits-with-optional-commas-and-decimal)(optional-suffix).
_NOTE_AMOUNT_PATTERN = re.compile(r"\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(k|K|m|M)?\b")


def _extract_decision_from_note_text(text: str) -> _NoteDecisionStatus | None:
    """Return the matched decision literal, or None if no pattern fires."""
    for status_literal, pattern in _NOTE_STATUS_PATTERNS.items():
        if pattern.search(text):
            return status_literal
    return None


def _extract_offer_amount_from_note_text(text: str) -> Decimal | None:
    """Best-effort dollar-amount extraction. ``45k`` -> 45000; ``1.5M`` ->
    1500000; ``$45,000`` -> 45000; ``1.35`` (a bare factor rate) returns
    None because the pattern requires either a thousands separator, a
    suffix, or 4+ digits to be interpreted as money. This is
    intentionally conservative — factor rates like ``1.35`` are common
    in funder notes and must not be misread as $1.
    """
    for match in _NOTE_AMOUNT_PATTERN.finditer(text):
        digits, decimals, suffix = match.group(1), match.group(2), match.group(3)
        try:
            base = Decimal(digits.replace(",", ""))
        except InvalidOperation:
            continue
        if decimals:
            base = base + Decimal(f"0.{decimals}")

        if suffix and suffix.lower() == "k":
            return base * Decimal("1000")
        if suffix and suffix.lower() == "m":
            return base * Decimal("1000000")

        # No suffix: only accept the value as money if it has a
        # thousands separator OR if it's an integer of 4+ digits.
        # Avoids reading factor rates like "1.35" as a dollar amount.
        if "," in digits:
            return base
        if decimals is None and len(digits) >= 4:
            return base
        # Bare 2-3 digit integers and decimals without suffix don't read
        # as money under this rule. Fall through and try the next match.
        continue
    return None


def _handle_note_created(
    *,
    event: dict[str, Any],
    merchants: MerchantRepository,
    funder_note_subs: FunderNoteSubmissionRepository,
    funders: FunderRepository,
    close_client: CloseClient,
    audit: AuditLog,
) -> None:
    """Auto-update a pending funder_note_submission from a Close Note.

    Quiet no-op when:
      * Event isn't an ``activity.note`` ``created``.
      * Lead doesn't map to a known merchant.
      * Merchant has zero pending submissions.
      * Note body contains no recognized decision phrase.
      * The same activity_id has been processed before (idempotency).

    Audits ``submission.auto_status_ambiguous`` and skips when the
    merchant has >1 pending submission — without a deterministic way to
    pin the note to a specific funder, the operator must update manually.
    """
    if event.get("action") != "created":
        return
    if event.get("object_type") != "activity.note":
        return

    activity_id_raw = event.get("object_id")
    if not isinstance(activity_id_raw, str) or not activity_id_raw:
        return

    data = event.get("data") or {}
    note_text_raw = data.get("note")
    if not isinstance(note_text_raw, str) or not note_text_raw.strip():
        return

    lead_id_raw = event.get("lead_id")
    if not isinstance(lead_id_raw, str) or not lead_id_raw:
        return

    merchant = merchants.find_by_close_lead_id(lead_id_raw)
    if merchant is None:
        return

    matched_status = _extract_decision_from_note_text(note_text_raw)
    if matched_status is None:
        return

    # Idempotency: skip if we've already auto-updated for this activity.
    prior_runs = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant.id,
        action="submission.auto_status_from_close_note",
        limit=500,
    )
    if any(r.get("details", {}).get("activity_id") == activity_id_raw for r in prior_runs):
        return

    try:
        all_rows = funder_note_subs.list_for_merchant(merchant.id, limit=200)
    except Exception:
        _log.warning(
            "close_webhook.note_submissions_fetch_failed lead_id=%s",
            lead_id_raw,
            exc_info=True,
        )
        return
    pending = [r for r in all_rows if r.status == "pending"]
    if not pending:
        return

    if len(pending) > 1:
        audit.record(
            actor="close_webhook",
            action="submission.auto_status_ambiguous",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "activity_id": activity_id_raw,
                "close_lead_id": lead_id_raw,
                "matched_status": matched_status,
                "pending_submission_ids": [str(r.id) for r in pending],
                "note_preview": note_text_raw[:200],
            },
        )
        return

    submission = pending[0]
    extracted_amount = _extract_offer_amount_from_note_text(note_text_raw)

    try:
        funder_note_subs.update_status(
            submission.id,
            status=matched_status,
            offer_amount=extracted_amount,
            notes=f"auto-status from Close note {activity_id_raw}",
        )
    except FunderNoteSubmissionNotFoundError:
        _log.warning(
            "close_webhook.note_submission_missing submission_id=%s",
            submission.id,
        )
        return

    audit.record(
        actor="close_webhook",
        action="submission.auto_status_from_close_note",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "activity_id": activity_id_raw,
            "close_lead_id": lead_id_raw,
            "submission_id": str(submission.id),
            "funder_id": str(submission.funder_id),
            "matched_status": matched_status,
            "offer_amount": str(extracted_amount) if extracted_amount is not None else None,
            "note_preview": note_text_raw[:200],
        },
    )

    if matched_status == "approved":
        _post_fund_the_deal_task(
            merchant=merchant,
            funder_id=submission.funder_id,
            funders=funders,
            close_client=close_client,
            close_lead_id=lead_id_raw,
            offer_amount=extracted_amount,
            audit=audit,
        )


def _post_fund_the_deal_task(
    *,
    merchant: MerchantRow,
    funder_id: UUID,
    funders: FunderRepository,
    close_client: CloseClient,
    close_lead_id: str,
    offer_amount: Decimal | None,
    audit: AuditLog,
) -> None:
    """Create a Close task prompting the operator to fund the approved deal."""
    try:
        funder_row = funders.get(funder_id)
        funder_name = funder_row.name
    except FunderNotFoundError:
        # Funder row vanished between submission and approval — surface
        # the gap but don't block the auto-status update.
        funder_name = "(funder lookup failed)"

    amount_phrase = f" for ${offer_amount:,.2f}" if offer_amount is not None else ""
    text = f"Fund the deal — {merchant.business_name} approved by {funder_name}{amount_phrase}"
    try:
        close_client.create_task(
            lead_id=close_lead_id,
            text=text,
            due_date=date.today(),
        )
    except CloseError as exc:
        audit.record(
            actor="close_webhook",
            action="close.task.fund_the_deal_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": close_lead_id,
                "funder_id": str(funder_id),
                "status_code": exc.status_code,
                "error": str(exc)[:200],
            },
        )
        return

    audit.record(
        actor="close_webhook",
        action="close.task.fund_the_deal_created",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "close_lead_id": close_lead_id,
            "funder_id": str(funder_id),
            "funder_name": funder_name,
            "task_text": text,
        },
    )


# ----------------------------------------------------------------------
# Feature D — merchant context refresh on Close webhook events
# ----------------------------------------------------------------------


def _refresh_close_context_best_effort(
    *,
    merchant_id: UUID,
    lead_id: str,
    lead_payload: dict[str, Any] | None,
    close_client: CloseClient,
    merchants: MerchantRepository,
    audit: AuditLog,
) -> None:
    """Call ``refresh_close_context_for_merchant``; swallow Close failures.

    Webhooks MUST stay 200-OK to Close (CLAUDE.md). A Close API failure
    on the context-refresh side path is logged + audited as
    ``merchant.close_context.refresh_failed`` and the webhook continues.

    ``lead_payload`` — when the caller has already fetched the Lead via
    ``close_client.get_lead`` we reuse it via a closure on the
    orchestrator's ``lead_fetcher`` kwarg. ``None`` means "fetch lazily".
    """
    if lead_payload is not None:
        lead_fetcher: Any = lambda _lead_id: lead_payload  # noqa: E731 — single-use closure
    else:
        lead_fetcher = None

    try:
        refresh_close_context_for_merchant(
            merchant_id,
            lead_id,
            close_client=close_client,
            merchants_repo=merchants,
            audit=audit,
            lead_fetcher=lead_fetcher,
        )
    except CloseError as exc:
        _log.warning(
            "close_webhook.close_context_refresh_failed lead_id=%s status=%s",
            lead_id,
            exc.status_code,
        )
        # Audit row carries no body content (PII rule). Per CLAUDE.md
        # "audit_log writes must succeed or the calling op fails" — a
        # failure to record this failure DOES propagate; the webhook
        # only swallows the Close-side error, not the audit-write
        # error. The auditor itself raising would surface as a 500
        # since the operator's ``AuditLog`` is supposed to be local
        # (Supabase insert) and a failure there is a real problem.
        audit.record(
            actor="close_webhook",
            action="merchant.close_context.refresh_failed",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "close_lead_id": lead_id,
                "status_code": exc.status_code,
                "error": str(exc)[:200],
            },
        )


# Activity event object_type values that should trigger a context
# refresh. Notes / calls / emails created or updated on the lead all
# carry information the LLM extraction prompt benefits from.
_REFRESH_TRIGGER_OBJECT_TYPES: frozenset[str] = frozenset(
    {"activity.note", "activity.call", "activity.email"}
)
_REFRESH_TRIGGER_ACTIONS: frozenset[str] = frozenset({"created", "updated"})


def _refresh_close_context_on_activity_event(
    *,
    event: dict[str, Any],
    close_client: CloseClient,
    merchants: MerchantRepository,
    audit: AuditLog,
) -> None:
    """Fire merchant-context refresh when a Close activity event resolves
    to a known merchant.

    Quiet no-op when:
      * Event isn't an activity-level note/call/email create/update.
      * Event has no resolvable ``lead_id``.
      * Lead doesn't map to a known merchant.

    Caller already audits ``close.webhook.received`` upstream, so the
    no-op paths leave no extra audit noise.
    """
    object_type = event.get("object_type")
    if object_type not in _REFRESH_TRIGGER_OBJECT_TYPES:
        return
    if event.get("action") not in _REFRESH_TRIGGER_ACTIONS:
        return

    lead_id_raw = event.get("lead_id")
    if not isinstance(lead_id_raw, str) or not lead_id_raw:
        return

    merchant = merchants.find_by_close_lead_id(lead_id_raw)
    if merchant is None:
        return

    _refresh_close_context_best_effort(
        merchant_id=merchant.id,
        lead_id=lead_id_raw,
        # Activity events don't carry the Lead payload — let the
        # orchestrator fetch lazily via close_client.get_lead.
        lead_payload=None,
        close_client=close_client,
        merchants=merchants,
        audit=audit,
    )


__all__ = ["router"]
