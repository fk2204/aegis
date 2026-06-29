"""Close lead intelligence — read-side aggregation for the dossier.

Pulls the operator-visible context that lives in Close (lead status,
description, agent notes, call dispositions, document folder URL) and
shapes it into a dossier-renderable object.

Read-only. No writes back to Close. The :func:`get_lead_intelligence`
entrypoint hits two endpoints (``/lead/{id}/`` and ``/activity/?lead_id=...``)
through the shared :class:`aegis.close.client.CloseClient`, then folds
the result into a :class:`CloseLeadIntelligence` Pydantic model.

PII discipline
--------------
Activity bodies (call notes, email subjects, note bodies) ARE PII at the
description level — the dossier renders them so the operator can see
them, but the audit-log writes from this module MUST NOT include note
content. We log a single ``close.intelligence_fetched`` row carrying
counts only (``activity_count``, ``call_count``) — never bodies.

Async wrapping
--------------
:class:`CloseClient` is synchronous (urllib + tenacity). The wider AEGIS
pattern wraps sync calls in ``asyncio.to_thread`` at the route level.
:func:`get_lead_intelligence` is sync per that convention; the dossier
route wraps the call in ``to_thread``.
"""

from __future__ import annotations

import re
import time
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from aegis.logger import get_logger

if TYPE_CHECKING:
    from aegis.audit import AuditLog
    from aegis.close.client import CloseClient

_log = get_logger(__name__)


# Activity types that show up in the dossier feed. ``status_change`` is
# emitted by Close on lead-status transitions; ``sms`` is rare but the
# wire shape carries it. Anything outside this set is ignored.
_ActivityType = Literal["call", "email", "note", "status_change", "sms"]
_KNOWN_ACTIVITY_TYPES: frozenset[str] = frozenset({"call", "email", "note", "status_change", "sms"})

# Bound on activities surfaced to the dossier. Close's activity endpoint
# returns up to 50 per page; we sort DESC by date and slice to 20 so the
# UI stays compact without losing the most-recent operator context.
_ACTIVITY_FEED_CAP: int = 20

# Cap on note bodies aggregated into the ``agent_notes`` block. Same
# reasoning — keep the dossier surface bounded.
_AGENT_NOTES_CAP: int = 10

# Document-folder URL extraction. Matches Zoho WorkDrive + Google Drive
# folder links pasted into the lead description by the broker. Non-greedy
# host match keeps the regex from spanning past whitespace.
_DOC_FOLDER_PATTERN = re.compile(
    r"https?://(?:workdrive\.zoho\.com|drive\.google\.com)\S+",
    re.IGNORECASE,
)

# Cache: lead_id -> (intelligence, expires_monotonic). Module-level dict
# protected by a Lock so the dossier route's ``asyncio.to_thread`` calls
# don't race on first-fetch. 15-minute TTL matches the SLA the dossier
# operator expects — they're not interactively chasing minute-by-minute
# changes in Close, but a stale call from an hour ago is misleading.
_CACHE_TTL_SECONDS: float = 15 * 60.0
_cache: dict[str, tuple[CloseLeadIntelligence, float]] = {}
_cache_lock = Lock()


class CloseActivity(BaseModel):
    """One row in the dossier's Close activity feed.

    All fields are derived from the Close ``/activity/`` payload, sliced
    to the keys the dossier renders. ``summary`` is the
    activity-type-appropriate body excerpt (call disposition note, email
    subject, note prefix). ``direction`` is only meaningful for
    ``call`` / ``email`` / ``sms``.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    type: _ActivityType
    date: str
    user_name: str
    summary: str
    direction: str | None = None


class CloseLeadIntelligence(BaseModel):
    """Aggregated Close-side context for one lead.

    Built once per dossier render and cached for 15 minutes. The dossier
    template hides the whole section when ``activities`` AND
    ``agent_notes`` are both empty so no-activity leads render no chrome.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    lead_id: str = Field(min_length=1)
    status_label: str = ""
    description: str = ""
    activities: list[CloseActivity] = Field(default_factory=list)
    call_count: int = 0
    last_contact: str | None = None
    agent_notes: list[str] = Field(default_factory=list)
    disqualified_reason: str | None = None
    document_folder_url: str | None = None


def _extract_document_folder_url(description: str) -> str | None:
    """Return the first Zoho WorkDrive / Google Drive URL in the
    description, or ``None`` when neither host appears."""
    if not description:
        return None
    match = _DOC_FOLDER_PATTERN.search(description)
    if match is None:
        return None
    # Strip common trailing punctuation that would otherwise glue onto
    # the URL (the broker often pastes "...folder/abc," mid-sentence).
    return match.group(0).rstrip(".,;:)>]")


def _activity_summary(raw: dict[str, Any], activity_type: str) -> str:
    """Build a one-line dossier summary for one activity row.

    Picks a body excerpt that matches what an operator looking at the
    Close timeline would recognize:
      * ``call`` — ``note`` (post-call operator disposition summary)
      * ``email`` — ``subject``, else first line of ``body_text``
      * ``note`` — first 200 chars of ``note``
      * ``status_change`` — ``new_status_label``
      * ``sms`` — ``text``

    Falls back to an empty string for unknown shapes — the dossier shows
    a row with the activity type + date but no summary line, which is
    still informative.
    """
    if activity_type == "call":
        note = raw.get("note")
        return str(note).strip() if note else ""
    if activity_type == "email":
        subject = raw.get("subject")
        if subject:
            return str(subject).strip()
        body = raw.get("body_text") or raw.get("body_preview") or ""
        first_line = str(body).strip().splitlines()[0] if body else ""
        return first_line[:200]
    if activity_type == "note":
        note = raw.get("note")
        if not note:
            return ""
        return str(note).strip()[:200]
    if activity_type == "status_change":
        return str(raw.get("new_status_label") or raw.get("status_label") or "").strip()
    if activity_type == "sms":
        text = raw.get("text") or raw.get("body") or ""
        return str(text).strip()[:200]
    return ""


def _activity_type_from_raw(raw: dict[str, Any]) -> str | None:
    """Map the Close ``_type`` field onto our internal taxonomy.

    Close uses PascalCase wire-side (``Note``, ``Call``, ``Email``,
    ``LeadStatusChange``, ``SMS``); we normalize to lowercase + the
    snake-case status_change form. Returns ``None`` for unknown types so
    the caller can drop them silently.
    """
    raw_type = raw.get("_type")
    if not isinstance(raw_type, str):
        return None
    lowered = raw_type.lower()
    if lowered in {"note", "call", "email", "sms"}:
        return lowered
    if lowered in {"leadstatuschange", "lead_status_change", "status_change"}:
        return "status_change"
    return None


def _build_activities(
    raw_items: list[dict[str, Any]],
) -> tuple[list[CloseActivity], int, str | None, list[str]]:
    """Walk the raw activity list once and produce:

    * ``activities`` — top-20 sorted DESC by date
    * ``call_count`` — total Call activities (not capped to 20)
    * ``last_contact`` — most-recent Call OR Email date, ``None`` if none
    * ``agent_notes`` — first 10 Note bodies in DESC-date order
    """
    typed: list[tuple[str, dict[str, Any], str]] = []
    call_count = 0
    last_contact: str | None = None
    notes: list[tuple[str, str]] = []  # (date, body)

    for raw in raw_items:
        atype = _activity_type_from_raw(raw)
        if atype is None or atype not in _KNOWN_ACTIVITY_TYPES:
            continue
        date_value = raw.get("date_created") or raw.get("activity_at") or ""
        typed.append((atype, raw, str(date_value)))
        if atype == "call":
            call_count += 1
            if last_contact is None or str(date_value) > last_contact:
                last_contact = str(date_value)
        elif atype == "email":
            if last_contact is None or str(date_value) > last_contact:
                last_contact = str(date_value)
        elif atype == "note":
            note_body = raw.get("note")
            if note_body:
                notes.append((str(date_value), str(note_body).strip()))

    # Sort by date DESC. Close timestamps are ISO-8601 with stable
    # lexicographic order, so a plain string sort is correct without
    # parsing into datetime.
    typed.sort(key=lambda t: t[2], reverse=True)

    activities: list[CloseActivity] = []
    for atype, raw, date_value in typed[:_ACTIVITY_FEED_CAP]:
        summary = _activity_summary(raw, atype)
        user_name = (
            raw.get("user_name") or raw.get("created_by_name") or raw.get("user_full_name") or ""
        )
        direction = raw.get("direction")
        activities.append(
            CloseActivity(
                # ``atype`` was vetted against ``_KNOWN_ACTIVITY_TYPES``
                # above; cast through ``Any`` to satisfy the Literal arg.
                type=cast(Any, atype),
                date=date_value,
                user_name=str(user_name).strip(),
                summary=summary,
                direction=str(direction).strip() if direction else None,
            )
        )

    notes.sort(key=lambda t: t[0], reverse=True)
    agent_notes = [body for _date, body in notes[:_AGENT_NOTES_CAP] if body]

    return activities, call_count, last_contact, agent_notes


def _disqualified_reason(status_label: str) -> str | None:
    """Return the status label when the lead is disqualified, else None.

    The dossier surfaces this prominently so the operator sees the
    Close-side disposition without re-reading every status row. Match is
    case-insensitive on ``disqualified`` so labels like
    ``"Disqualified - bad fit"`` or ``"DISQUALIFIED"`` all trip the flag.
    """
    if not status_label:
        return None
    if "disqualified" in status_label.lower():
        return status_label
    return None


def get_lead_intelligence(
    close_client: CloseClient,
    lead_id: str,
    *,
    audit: AuditLog | None = None,
) -> CloseLeadIntelligence:
    """Read Close lead context + activities and shape into a dossier model.

    Result is cached per-lead for 15 minutes. The cache is module-scoped
    so it survives across requests within the same worker process; new
    workers warm their own cache on first hit.

    PII discipline: the audit row written by this function (when
    ``audit`` is supplied) carries counts only — no note bodies, no
    email subjects, no call dispositions. The bodies still flow into the
    return value because the dossier renders them.

    Network failures bubble up as :class:`aegis.close.client.CloseError`
    subclasses — the caller (the dossier route) is responsible for
    catching and degrading to ``close_intel = None``.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(lead_id)
        if cached is not None and cached[1] > now:
            return cached[0]

    lead_payload = close_client.get_lead(lead_id)
    status_label = str(lead_payload.get("status_label") or "")
    description = str(lead_payload.get("description") or "")

    raw_activities = close_client.get_lead_activities(lead_id)
    activities, call_count, last_contact, agent_notes = _build_activities(raw_activities)

    intel = CloseLeadIntelligence(
        lead_id=lead_id,
        status_label=status_label,
        description=description,
        activities=activities,
        call_count=call_count,
        last_contact=last_contact,
        agent_notes=agent_notes,
        disqualified_reason=_disqualified_reason(status_label),
        document_folder_url=_extract_document_folder_url(description),
    )

    expires_at = time.monotonic() + _CACHE_TTL_SECONDS
    with _cache_lock:
        _cache[lead_id] = (intel, expires_at)

    if audit is not None:
        try:
            audit.record(
                actor="close_client",
                action="close.intelligence_fetched",
                details={
                    "lead_id": lead_id,
                    "activity_count": len(activities),
                    "call_count": call_count,
                },
            )
        except Exception:
            _log.warning("close.intelligence_audit_write_failed", exc_info=True)

    return intel


def _clear_cache_for_tests() -> None:
    """Drop the module-level cache. Test-only — production code never
    needs to invalidate (the 15-minute TTL is the only invalidation
    surface)."""
    with _cache_lock:
        _cache.clear()


__all__ = [
    "CloseActivity",
    "CloseLeadIntelligence",
    "get_lead_intelligence",
]
