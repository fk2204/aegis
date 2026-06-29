"""Auto-create Close tasks when AEGIS hits a compliance gate.

Build-plan item 7.3. Three gate trigger points create a Close Lead Task
the operator clears manually:

* **OFAC block** — :func:`refresh_ofac_for_merchant` writes
  ``compliance.ofac_block`` after a fuzzy-match hit on SDN /
  Consolidated; this module is invoked AFTER that audit row lands.
* **Bankruptcy block** — :func:`refresh_bankruptcy_for_merchant` writes
  ``compliance.bankruptcy_block`` after CourtListener finds an active
  Chapter 7; same hook posture.
* **Licensing required** — the dossier-render path's
  :func:`evaluate_license_gate` returns ``required=True``; the
  merchants router invokes the dedicated wrapper here to file an
  operator task pointing at the state portal URL.

Failure posture
---------------
A Close API outage MUST NOT block the gate decision itself. Every
caller wraps the task-create in try/except — failures log + write a
``close.task.create_failed`` audit row but the gate / refresh
completes normally. The audit log is the durable signal; operators
see the gate (red banner / disabled Submit) regardless of whether
the Close-side task got filed.

Idempotency
-----------
Close does NOT de-dupe — same payload twice creates two tasks. For
OFAC + bankruptcy the upstream refresh functions already idempotent-
gate via TTL (``ensure_*_check``) so re-firing across screens isn't
expected. For the licensing path (evaluated on every dossier render)
the caller checks the audit log for a prior
``close.task.compliance_gate_created`` row before invoking us — this
module does NOT re-check.

PII discipline
--------------
Audit ``details`` carry ``gate_type``, ``close_lead_id``,
``merchant_id``, the returned ``task_id_from_close``, and gate-
specific structural fields (the matched SDN entry id, bankruptcy
chapter, state + license type). The merchant's business_name /
owner_name appear in the Close-side task text — operators need that
context to action the task — but are NEVER duplicated into audit
``details``. The project logger's mask-by-key/value pattern catches
incidental leaks; this module's discipline is "structural fields
only" in audit rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.close.client import CloseClient, CloseError
from aegis.logger import get_logger

if TYPE_CHECKING:
    from aegis.audit import AuditLog
    from aegis.merchants.models import MerchantRow

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class ComplianceGateType(StrEnum):
    """Closed set of compliance gates that file a Close task.

    Mirrors the three gate-trigger functions enumerated in build-plan
    7.3. Used as both the enum carried in audit ``details`` and the
    selector for the task-text builder. String-valued so the audit-log
    JSON shape is stable across releases.
    """

    OFAC_BLOCK = "ofac_block"
    BANKRUPTCY_BLOCK = "bankruptcy_block"
    LICENSE_REQUIRED = "license_required"


class OFACGateDetails(BaseModel):
    """Structured detail payload for an OFAC-block task.

    ``sdn_name`` is the public-record matched SDN entry name (already
    safe to surface — SDN list is published by Treasury). The merchant
    business name is supplied separately by the caller via the
    :class:`MerchantRow` argument so the task text can read "OFAC block
    on <Business> — matched <SDN name>".
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sdn_name: str = Field(min_length=1)


class BankruptcyGateDetails(BaseModel):
    """Structured detail payload for a bankruptcy-block task.

    ``chapter`` mirrors the value persisted on
    ``merchants.bankruptcy_chapter`` (today only "7" trips the block;
    the enum-typed field keeps room for future chapters without
    requiring a Pydantic schema change). ``case_count`` carries the
    number of active cases CourtListener returned; ``court`` is the
    short identifier of the most-recent case's court.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    chapter: str = Field(min_length=1)
    case_count: int = Field(ge=1)
    court: str | None = None


class LicenseGateDetails(BaseModel):
    """Structured detail payload for a license-required task.

    ``state`` is the USPS 2-letter code (uppercase). ``license_type``
    is the operator-facing label from
    ``LICENSE_INDUSTRY_LABELS`` (e.g. "General Contractor", "Pharmacy").
    ``portal_url`` is the verified state portal URL surfaced on the
    dossier banner.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    state: str = Field(min_length=2, max_length=2)
    license_type: str = Field(min_length=1)
    portal_url: str = Field(min_length=1)


# Pydantic shape for the Close /api/v1/task/ POST body. Per the Close
# docs the body is ``{"_type": "lead", "lead_id", "text", "date",
# "assigned_to"}``; we never send raw dicts across the boundary so
# every field gets type-checked once at construction time.
class CloseTaskPayload(BaseModel):
    """Typed payload for ``POST /api/v1/task/``.

    Built locally and immediately serialized via :meth:`build_payload`
    so the :class:`CloseClient.create_task` call stays str-keyed. The
    sole reason this model exists is the "no raw dicts crossing the
    Close API boundary" rule from build-plan 7.3 — we keep the body
    shape explicit and Pydantic-validated rather than constructing a
    dict inline at the call site.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    lead_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    due_date: date
    assigned_to: str | None = None


# Default lead-time for the operator-action due date. One business day
# is conservative — OFAC and bankruptcy blocks need same-week action;
# license verification is rarely time-pressured but a 1-day default
# matches the urgency the dossier banner signals.
DEFAULT_DUE_IN_DAYS: int = 1


def create_compliance_gate_task(
    *,
    merchant: MerchantRow,
    gate_type: ComplianceGateType,
    details: OFACGateDetails | BankruptcyGateDetails | LicenseGateDetails,
    client: CloseClient,
    audit: AuditLog,
    due_in_days: int = DEFAULT_DUE_IN_DAYS,
    now: datetime | None = None,
) -> str | None:
    """Create a Close task for the merchant's lead, audit every path.

    Returns the Close-side task id when created, ``None`` when skipped
    (no ``close_lead_id``) or when the Close call failed (the audit
    row carries the failure detail).

    NEVER raises — Close API outages are wrapped + audited. The
    caller's gate decision is independent of this function's success.

    Audit rows written (exactly one per call):

    * ``close.task.compliance_gate_created`` — task POSTed, ``task_id``
      captured.
    * ``close.task.skipped_no_lead`` — merchant has no
      ``close_lead_id``; we cannot file a Close-side task.
    * ``close.task.create_failed`` — Close API raised; the gate
      decision still completes upstream.
    """
    now_dt = now or datetime.now(UTC)
    due_date_value = (now_dt + timedelta(days=due_in_days)).date()

    if not merchant.close_lead_id:
        audit.record(
            actor="system",
            action="close.task.skipped_no_lead",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "gate_type": gate_type.value,
                "merchant_id": str(merchant.id),
                "reason": "merchant.close_lead_id is None",
            },
        )
        _log.info(
            "close.task.skipped_no_lead merchant_id=%s gate_type=%s",
            merchant.id,
            gate_type.value,
        )
        return None

    text = _build_task_text(merchant=merchant, gate_type=gate_type, details=details)
    assigned_to = _resolve_assignee(
        client=client,
        lead_id=merchant.close_lead_id,
        merchant_id=merchant.id,
        gate_type=gate_type,
        audit=audit,
    )

    payload = CloseTaskPayload(
        lead_id=merchant.close_lead_id,
        text=text,
        due_date=due_date_value,
        assigned_to=assigned_to,
    )

    try:
        response = client.create_task(
            lead_id=payload.lead_id,
            text=payload.text,
            due_date=payload.due_date,
            assigned_to=payload.assigned_to,
        )
    except CloseError as exc:
        audit.record(
            actor="system",
            action="close.task.create_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "gate_type": gate_type.value,
                "merchant_id": str(merchant.id),
                "close_lead_id": merchant.close_lead_id,
                "status_code": exc.status_code,
                "error": type(exc).__name__,
            },
        )
        _log.warning(
            "close.task.create_failed merchant_id=%s gate_type=%s status=%s error=%s",
            merchant.id,
            gate_type.value,
            exc.status_code,
            type(exc).__name__,
        )
        return None

    task_id = _extract_task_id(response)
    audit.record(
        actor="system",
        action="close.task.compliance_gate_created",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "gate_type": gate_type.value,
            "merchant_id": str(merchant.id),
            "close_lead_id": merchant.close_lead_id,
            "task_id_from_close": task_id,
            "assigned_to": assigned_to,
            "due_date": due_date_value.isoformat(),
        },
    )
    _log.info(
        "close.task.compliance_gate_created merchant_id=%s gate_type=%s task_id=%s",
        merchant.id,
        gate_type.value,
        task_id,
    )
    return task_id


def has_open_gate_task(
    *,
    audit: AuditLog,
    merchant_id: UUID,
    gate_type: ComplianceGateType,
) -> bool:
    """Idempotency check — has a prior task for this gate been filed?

    Reads ``close.task.compliance_gate_created`` rows on the merchant
    and returns True when at least one carries the matching
    ``gate_type``. Used by the license-gate caller to avoid re-creating
    a task on every dossier render.

    The OFAC + bankruptcy callers do NOT need to call this — the
    upstream refresh functions are TTL-gated (30 days / first-screen)
    so the block audit row only fires on rescreens, which is the
    intended retry cadence.
    """
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant_id,
        action="close.task.compliance_gate_created",
        limit=50,
    )
    for row in rows:
        details = row.get("details") or {}
        if isinstance(details, dict) and details.get("gate_type") == gate_type.value:
            return True
    return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_task_text(
    *,
    merchant: MerchantRow,
    gate_type: ComplianceGateType,
    details: OFACGateDetails | BankruptcyGateDetails | LicenseGateDetails,
) -> str:
    """Compose the Close-side task text from the gate-specific details.

    Text shape per gate (one line each — Close renders the operator's
    task list as a checklist):

    * OFAC:    ``OFAC block on <Business> — verify identity before proceeding (matched: <SDN>)``
    * BANKRPT: ``Bankruptcy block on <Business> — chapter <N> (<court>, <K> case(s)) —
      review before proceeding``
    * LICENSE: ``License verification required — <state> <license_type>: <portal_url>``

    The merchant's business name appears in the Close task text — the
    operator needs the lead-level context to action the task. The
    fallback "(unnamed merchant)" handles the early provisional row
    state where ``business_name`` is None.
    """
    business_name = merchant.business_name or "(unnamed merchant)"

    if gate_type is ComplianceGateType.OFAC_BLOCK:
        if not isinstance(details, OFACGateDetails):
            raise TypeError(f"OFAC_BLOCK requires OFACGateDetails, got {type(details).__name__}")
        return (
            f"OFAC block on {business_name} — verify identity before "
            f"proceeding (matched: {details.sdn_name})"
        )

    if gate_type is ComplianceGateType.BANKRUPTCY_BLOCK:
        if not isinstance(details, BankruptcyGateDetails):
            raise TypeError(
                f"BANKRUPTCY_BLOCK requires BankruptcyGateDetails, got {type(details).__name__}"
            )
        court_clause = f" ({details.court}, " if details.court else " ("
        plural = "case" if details.case_count == 1 else "cases"
        return (
            f"Bankruptcy block on {business_name} — chapter "
            f"{details.chapter}{court_clause}{details.case_count} {plural})"
            " — review before proceeding"
        )

    if gate_type is ComplianceGateType.LICENSE_REQUIRED:
        if not isinstance(details, LicenseGateDetails):
            raise TypeError(
                f"LICENSE_REQUIRED requires LicenseGateDetails, got {type(details).__name__}"
            )
        return (
            f"License verification required — {details.state} "
            f"{details.license_type}: {details.portal_url}"
        )

    # mypy: ComplianceGateType is closed; this branch is unreachable.
    raise ValueError(f"unhandled gate_type: {gate_type!r}")


def _resolve_assignee(
    *,
    client: CloseClient,
    lead_id: str,
    merchant_id: UUID,
    gate_type: ComplianceGateType,
    audit: AuditLog,
) -> str | None:
    """Read the lead's ``assigned_to`` user id from Close.

    Close returns ``assigned_to`` (or ``assigned_to_user_id`` on some
    older payloads) on the Lead GET. When absent (lead has no owner)
    we omit the field on the task POST so Close defaults to the API
    key's owning user — operator-acceptable fallback.

    A Close GET failure here MUST NOT block the task creation — log +
    audit + return None so the task still files (Close defaults the
    assignee). The audit row is best-effort; a Close outage at this
    stage will surface again on the create_task path with a richer
    error row.
    """
    try:
        lead = client.get_lead(lead_id)
    except CloseError as exc:
        audit.record(
            actor="system",
            action="close.task.assignee_lookup_failed",
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "gate_type": gate_type.value,
                "close_lead_id": lead_id,
                "status_code": exc.status_code,
                "error": type(exc).__name__,
            },
        )
        _log.warning(
            "close.task.assignee_lookup_failed lead_id=%s status=%s error=%s",
            lead_id,
            exc.status_code,
            type(exc).__name__,
        )
        return None

    # Close's lead payload uses ``assigned_to`` (a single user id). Some
    # older API shapes also expose ``assigned_to_user_id``; we accept
    # either as a defensive measure. Both are top-level strings.
    candidate = lead.get("assigned_to") or lead.get("assigned_to_user_id")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return None


def _extract_task_id(response: dict[str, Any]) -> str | None:
    """Pull the ``id`` field off the Close /api/v1/task/ POST response.

    Close returns the full created Task object. Defensive on shape:
    a missing or non-string ``id`` returns None rather than raising,
    so a future Close API change can't fail the audit-write that
    follows this lookup.
    """
    task_id = response.get("id")
    if isinstance(task_id, str) and task_id:
        return task_id
    return None


__all__ = [
    "DEFAULT_DUE_IN_DAYS",
    "BankruptcyGateDetails",
    "CloseTaskPayload",
    "ComplianceGateType",
    "LicenseGateDetails",
    "OFACGateDetails",
    "create_compliance_gate_task",
    "has_open_gate_task",
]
