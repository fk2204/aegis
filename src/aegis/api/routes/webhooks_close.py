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
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_funder_note_submission_repository,
    get_merchant_repository,
)
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
from aegis.close.orchestration import enqueue_close_orchestration
from aegis.config import Settings, get_settings
from aegis.funder_note_submissions import (
    FunderNoteSubmissionNotFoundError,
    FunderNoteSubmissionRepository,
    FunderNoteSubmissionStatus,
)
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
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
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

    _upsert_merchant_from_lead(
        lead=lead,
        close_lead_id=lead_id,
        close_opportunity_id=opportunity_id,
        merchants=merchants,
        audit=audit,
    )

    # Stage 5 — fire-and-forget the attachment-orchestration arq job.
    # Failure to enqueue (Redis blip etc.) audits but does NOT 5xx the
    # webhook: Close will retry within 72 hours and the merchant
    # upsert is already idempotent, so a self-healing re-run is cheap.
    # Converting transient enqueue failures to 5xx would just generate
    # noise without adding any safety.
    merchant_for_audit = merchants.find_by_close_lead_id(lead_id)
    await enqueue_close_orchestration(
        request=request,
        close_lead_id=lead_id,
        merchant_id=merchant_for_audit.id if merchant_for_audit is not None else None,
        audit=audit,
        trigger="webhook",
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_WEBHOOK_SECRET is not valid hex",
        ) from None

    data = timestamp_str.encode("utf-8") + raw_body
    expected = hmac.new(secret_bytes, data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented_sig, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


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


def _upsert_merchant_from_lead(
    *,
    lead: dict[str, Any],
    close_lead_id: str,
    close_opportunity_id: str | None,
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
            close_opportunity_id=close_opportunity_id,
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
    business_name = legal_name or lead.get("display_name") or lead.get("name") or ""

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
        naics = industry_to_naics(industry_choice if isinstance(industry_choice, str) else None)

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
            raise FieldMapError(f"time_in_business_months not an int: {tib_raw!r}") from exc

    return {
        "business_name": str(business_name),
        "dba": _str_or_none(get_custom_field(lead, "dba_name")),
        "ein": _str_or_none(get_custom_field(lead, "ein")),
        "owner_name": str(get_custom_field(lead, "owner_name") or ""),
        "state": state,
        "industry_naics": naics,
        # Persist the raw Lead-side Industry choice string alongside
        # the derived NAICS. Drives ``aegis.scoring_v2.industry`` tier
        # lookup on the dossier + Track B band adjustment. Migration 055.
        "industry_choice": (
            industry_choice if isinstance(industry_choice, str) and industry_choice else None
        ),
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
