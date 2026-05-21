"""POST /webhooks/close — receive Close ``opportunity.updated`` events.

Two security layers (matches the pattern in ``webhooks_zoho`` and
``funder_replies``):

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
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from aegis.api.deps import get_audit, get_close_client, get_merchant_repository
from aegis.audit import AuditLog
from aegis.close.client import CloseClient, CloseError
from aegis.close.field_map import (
    FieldMapError,
    get_custom_field,
    industry_to_naics,
    normalize_entity_type,
    parse_fico_range,
    parse_money,
    resolve_entity_type,
)
from aegis.config import get_settings
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantRepository

router = APIRouter(prefix="/webhooks/close", tags=["webhooks"])

_log = get_logger(__name__)

WEBHOOK_FRESHNESS_SECONDS = 5 * 60


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def close_webhook(
    request: Request,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
) -> None:
    raw_body = await request.body()

    # Stage 1 — verify signature + freshness. 401 on either fail. No body
    # parsing before this point; HMAC must compute over the raw bytes.
    _verify_signature(request.headers, raw_body)

    # Stage 2 — parse JSON. 400 on malformed.
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid json: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="webhook body must be a JSON object",
        )

    event = payload.get("event")
    if not isinstance(event, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="webhook body missing 'event' object",
        )

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

    if not matches:
        # Filtered out. The audit row stands; nothing else to do.
        return None

    # Stage 4 — pull the Lead and upsert the merchant (idempotent).
    lead_id = event.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="event matches trigger filter but lead_id is missing",
        )

    try:
        lead = close_client.get_lead(lead_id)
    except CloseError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"close_get_lead_failed: {exc}",
        ) from exc

    _upsert_merchant_from_lead(
        lead=lead,
        close_lead_id=lead_id,
        merchants=merchants,
        audit=audit,
    )
    return None


# ----------------------------------------------------------------------
# Signature + freshness
# ----------------------------------------------------------------------


def _verify_signature(headers: Any, raw_body: bytes) -> None:  # noqa: ANN401 — FastAPI Headers proxy
    """Reject the request unless the close-sig-hash + close-sig-timestamp
    headers carry a valid HMAC over (timestamp + raw_body) and the
    timestamp is within the 5-minute freshness window. 401 on any fail
    with a generic body — don't leak which check failed.
    """
    settings = get_settings()
    if settings.close_webhook_secret is None:
        # Fail-closed: an unconfigured secret must not silently accept
        # signed traffic. 503 makes it clear the integration isn't
        # ready, vs 401 which would suggest a key mismatch.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_WEBHOOK_SECRET is not configured",
        )

    presented_sig = headers.get("close-sig-hash", "")
    timestamp_str = headers.get("close-sig-timestamp", "")
    if not presented_sig or not timestamp_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )

    # Freshness — Close sends Unix epoch seconds as the timestamp; some
    # client variants emit an ISO 8601 form. Accept either.
    try:
        ts_dt = _parse_timestamp(timestamp_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from None

    age = abs((datetime.now(UTC) - ts_dt).total_seconds())
    if age > WEBHOOK_FRESHNESS_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )

    # HMAC — Close gives the signature_key hex-encoded; the HMAC input
    # is `close-sig-timestamp + raw_body` (concatenated, no separator).
    secret_hex = settings.close_webhook_secret.get_secret_value()
    try:
        secret_bytes = bytes.fromhex(secret_hex)
    except ValueError:
        # Configuration error rather than auth error — the operator's
        # secret is malformed. Still don't leak it; 503 + generic detail.
        _log.error("close_webhook_secret_not_valid_hex")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_WEBHOOK_SECRET is not valid hex",
        ) from None

    data = timestamp_str.encode("utf-8") + raw_body
    expected = hmac.new(secret_bytes, data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented_sig, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
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
# Merchant upsert
# ----------------------------------------------------------------------


def _upsert_merchant_from_lead(
    *,
    lead: dict[str, Any],
    close_lead_id: str,
    merchants: MerchantRepository,
    audit: AuditLog,
) -> None:
    """Read-before-write merchant upsert keyed by close_lead_id.

    Idempotency rule (design doc guarantee #2): if a merchant row already
    exists with this close_lead_id AND none of the AEGIS-side fields
    derived from the Lead would change, do NOT write. This keeps
    redelivered events from generating noise on the merchants row's
    updated_at timestamp.

    Parse failures from field_map (e.g. unknown FICO Range, unknown
    Industry) bubble up as HTTPException(400) — the operator must see
    that surprise in Close, not have it silently swallowed.
    """
    try:
        new_fields = _lead_to_merchant_fields(lead, close_lead_id, audit)
    except FieldMapError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"close_lead_field_parse_failed: {exc}",
        ) from exc

    existing = merchants.find_by_close_lead_id(close_lead_id)

    if existing is None:
        new_merchant = MerchantRow(
            id=uuid4(),
            close_lead_id=close_lead_id,
            **new_fields,
        )
        merchants.upsert(new_merchant)
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
        return

    # Read-before-write diff. If nothing changed, skip the write.
    diff = {
        key: val
        for key, val in new_fields.items()
        if getattr(existing, key, None) != val
    }
    if not diff:
        # Pure idempotent redelivery. No write, no audit-row noise.
        return

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


def _lead_to_merchant_fields(
    lead: dict[str, Any],
    close_lead_id: str,
    audit: AuditLog,
) -> dict[str, Any]:
    """Translate a Close Lead payload into a MerchantRow-field dict.

    All Close-specific cf_<id> lookups go through field_map.get_custom_field
    so the cf_id table stays in one place. Pure (no DB, no HTTP).
    """
    legal_name = get_custom_field(lead, "legal_name")
    business_name = (
        legal_name
        or lead.get("display_name")
        or lead.get("name")
        or ""
    )

    state_raw = get_custom_field(lead, "state")
    state = (state_raw or "").upper() if isinstance(state_raw, str) else ""

    industry_choice = get_custom_field(lead, "industry")
    naics_explicit = get_custom_field(lead, "naics_code")
    # Prefer the operator's explicit NAICS Code when set; else derive
    # from Industry choice.
    naics: str | None
    if isinstance(naics_explicit, str) and naics_explicit.strip():
        naics = naics_explicit.strip()
    else:
        naics = industry_to_naics(
            industry_choice if isinstance(industry_choice, str) else None
        )

    raw_entity = resolve_entity_type(
        entity_type_a=get_custom_field(lead, "entity_type_a"),
        entity_type_b=get_custom_field(lead, "entity_type_b"),
        close_lead_id=close_lead_id,
        audit=audit,
    )
    entity = normalize_entity_type(raw_entity)

    tib_raw = get_custom_field(lead, "time_in_business_months")
    tib_months: int | None
    if tib_raw is None or tib_raw == "":
        tib_months = None
    else:
        try:
            tib_months = int(tib_raw)
        except (ValueError, TypeError) as exc:
            raise FieldMapError(
                f"time_in_business_months not an int: {tib_raw!r}"
            ) from exc

    return {
        "business_name": str(business_name),
        "dba": _str_or_none(get_custom_field(lead, "dba_name")),
        "ein": _str_or_none(get_custom_field(lead, "ein")),
        "owner_name": str(get_custom_field(lead, "owner_name") or ""),
        "state": state,
        "industry_naics": naics,
        "time_in_business_months": tib_months,
        "credit_score": parse_fico_range(
            get_custom_field(lead, "fico_range")
            if isinstance(get_custom_field(lead, "fico_range"), str)
            else None
        ),
        "requested_amount": parse_money(get_custom_field(lead, "requested_amount")),
        "entity_type": entity,
    }


def _str_or_none(value: Any) -> str | None:  # noqa: ANN401 — Close custom-field values are heterogeneous
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["router"]
