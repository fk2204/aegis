"""Disclosure render-event log (U16 — migration 042).

U3 (commit ``924d799``) caught ``APRDisclosureError`` in
``api/routes/disclosures.py`` and surfaced an in-memory
``disclosure_status="needs_review"`` payload but explicitly deferred
persistence ("Schema-level disclosure_status is a separate ticket
pending operator decision on the persistence shape").

This module owns the write path for ``disclosure_render_events``
(migration 042) — one row per render attempt carrying:

  * the deal/merchant the render was for (both nullable; the route may
    catch an APRDisclosureError before merchant lookup),
  * the state + template_path when known,
  * the render outcome ``status`` (``ok`` / ``needs_review`` /
    ``apr_compute_failed`` / future cases),
  * a short ``status_reason``,
  * non-PII numeric ``details`` (deal-id-ish reference, APR inputs),
  * an optional ``recipient_email`` when the render led to a transmission.

Two implementations of the ``DisclosureRenderEventRepository`` Protocol
mirror ``compliance/transmission.py``:

  * ``InMemoryDisclosureRenderEventRepository`` — list-backed; used by
    tests and the in-memory backend.
  * ``SupabaseDisclosureRenderEventRepository`` — writes one row per
    ``record()`` call. Insert failure raises
    ``DisclosureRenderEventWriteError`` so the calling pipeline can fail
    rather than continuing with no render-event trail.

Scope note (per ``.claude/rules/compliance.md``): this is AEGIS's
internal pre-flight render log, NOT a regulator-facing status surface.
The funder owns regulator-facing disclosure issuance. The four-year
retention floor on ``disclosure_transmissions`` (migration 036) does
NOT apply here.

Audit-log coupling
------------------
``record_disclosure_render_event(...)`` writes the render-event row
AND a paired ``audit_log`` row with ``action='aegis_disclosure_render_event'``
so the durable audit trail captures the event regardless of which
side a reader queries from. Per CLAUDE.md "audit-write failures FAIL
the operation"; the audit_log write is the existing U3 contract and
must continue firing — this helper appends a render-event row alongside
it (the caller passes the audit_log dependency in and the helper writes
both).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from aegis.audit import AuditLog
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


# --- status constants -------------------------------------------------------
#
# Free-text VARCHAR(32) on the DB side; pinned constants on the Python
# side so callers cannot accidentally drift. New statuses go here AND
# in the migration-042 header comment.

RENDER_EVENT_STATUS_OK = "ok"
"""Render succeeded. May carry ``recipient_email`` when transmission followed."""

RENDER_EVENT_STATUS_NEEDS_REVIEW = "needs_review"
"""Render produced a known-bad output and AEGIS held the disclosure.

Currently the generic umbrella for held-for-operator-review events.
``apr_compute_failed`` is the only specific case wired today, but
future detectors (zero-balance disbursement_date, missing required
template field) can emit the generic ``needs_review`` status when no
more-specific code applies.
"""

RENDER_EVENT_STATUS_APR_FAILED = "apr_compute_failed"
"""APR compute (``compliance.apr.calculate_apr``) failed to converge."""

RENDER_EVENT_STATUS_TEMPLATE_FAILED = "template_render_failed"
"""Jinja2 template render itself raised. Future-use; not emitted today."""


_VALID_STATUSES = frozenset(
    {
        RENDER_EVENT_STATUS_OK,
        RENDER_EVENT_STATUS_NEEDS_REVIEW,
        RENDER_EVENT_STATUS_APR_FAILED,
        RENDER_EVENT_STATUS_TEMPLATE_FAILED,
    }
)


class DisclosureRenderEventWriteError(RuntimeError):
    """Raised when a render-event row could not be persisted.

    Mirrors ``DisclosureTransmissionWriteError`` semantics: a write
    failure halts the calling operation rather than continuing with no
    render-event trail.
    """


class DisclosureRenderEventRecord(BaseModel):
    """One render-event row. Pydantic so callers cannot pass loose dicts."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: UUID
    deal_id: UUID | None
    merchant_id: UUID | None
    state: str | None
    template_path: str | None
    status: str
    status_reason: str | None
    details: dict[str, Any] | None
    recipient_email: str | None
    rendered_at: datetime
    rendered_by: str | None
    metadata: dict[str, Any] | None


class DisclosureRenderEventRepository(Protocol):
    """Append-only interface. Implementations must raise on write failure."""

    def record(
        self,
        *,
        deal_id: UUID | None,
        merchant_id: UUID | None,
        state: str | None,
        template_path: str | None,
        status: str,
        status_reason: str | None,
        details: dict[str, Any] | None,
        recipient_email: str | None,
        rendered_by: str | None,
        rendered_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DisclosureRenderEventRecord: ...

    def list_by_status(
        self,
        *,
        status: str,
        limit: int = 50,
    ) -> list[DisclosureRenderEventRecord]:
        """Return render events with the given ``status``, newest first.

        Drives the operator-facing triage queue: "show me every
        needs_review event in the last hour." Bounded ``limit`` keeps
        reads predictable.
        """

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
        status: str | None = None,
        limit: int = 500,
    ) -> list[DisclosureRenderEventRecord]:
        """Return render events whose ``rendered_at`` falls in the window.

        Drives the operator-facing triage page + the portfolio KPI tile.
        ``to_date`` is inclusive — implementations expand it to
        ``to_date + 1 day at 00:00`` (or equivalent) so a row stamped at
        ``23:59:59`` on ``to_date`` is included. Sorted newest-first.

        ``status`` filters to a single status when set; ``None`` returns
        all statuses.
        """

    def get(self, event_id: UUID) -> DisclosureRenderEventRecord | None:
        """Return the single render event with ``event_id`` or ``None``.

        Drives the operator-facing detail subroute at
        ``/ui/disclosure-events/{id}``. ``None`` is the not-found signal
        the route turns into a 404.
        """


def _normalize_state(state: str | None) -> str | None:
    """USPS 2-letter uppercase, or ``None`` when caller did not supply one."""
    if state is None:
        return None
    upper = state.upper()
    if len(upper) != 2:
        raise ValueError(f"state must be a 2-letter USPS code, got {state!r}")
    return upper


def _validate_status(status: str) -> str:
    """Defensive: accept known statuses only. Caller bug otherwise."""
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_STATUSES)!r}, got {status!r}"
        )
    return status


class InMemoryDisclosureRenderEventRepository:
    """List-backed implementation. Used by tests and the memory backend."""

    def __init__(self) -> None:
        self.rows: list[DisclosureRenderEventRecord] = []

    def record(
        self,
        *,
        deal_id: UUID | None,
        merchant_id: UUID | None,
        state: str | None,
        template_path: str | None,
        status: str,
        status_reason: str | None,
        details: dict[str, Any] | None,
        recipient_email: str | None,
        rendered_by: str | None,
        rendered_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DisclosureRenderEventRecord:
        record = DisclosureRenderEventRecord(
            id=uuid4(),
            deal_id=deal_id,
            merchant_id=merchant_id,
            state=_normalize_state(state),
            template_path=template_path,
            status=_validate_status(status),
            status_reason=status_reason,
            details=details,
            recipient_email=recipient_email,
            rendered_at=rendered_at or datetime.now(UTC),
            rendered_by=rendered_by,
            metadata=metadata,
        )
        self.rows.append(record)
        return record

    def list_by_status(
        self,
        *,
        status: str,
        limit: int = 50,
    ) -> list[DisclosureRenderEventRecord]:
        _validate_status(status)
        matches = [r for r in self.rows if r.status == status]
        matches.sort(key=lambda r: r.rendered_at, reverse=True)
        return matches[: max(0, limit)]

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
        status: str | None = None,
        limit: int = 500,
    ) -> list[DisclosureRenderEventRecord]:
        if status is not None:
            _validate_status(status)
        # Inclusive window — every wall-clock instant on ``to_date`` is
        # in-range, so the comparison is against ``to_date`` end-of-day.
        upper = datetime.combine(to_date, time.max, tzinfo=UTC)
        lower = datetime.combine(from_date, time.min, tzinfo=UTC)
        matches: list[DisclosureRenderEventRecord] = []
        for r in self.rows:
            ts = r.rendered_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts < lower or ts > upper:
                continue
            if status is not None and r.status != status:
                continue
            matches.append(r)
        matches.sort(key=lambda r: r.rendered_at, reverse=True)
        return matches[: max(0, limit)]

    def get(self, event_id: UUID) -> DisclosureRenderEventRecord | None:
        for r in self.rows:
            if r.id == event_id:
                return r
        return None


class SupabaseDisclosureRenderEventRepository:
    """Persistence backed by Postgres ``disclosure_render_events`` table.

    Mirrors the in-memory contract; the table has no STORED
    ``retention_until`` because this is internal pre-flight data, not
    regulator-facing.
    """

    def record(
        self,
        *,
        deal_id: UUID | None,
        merchant_id: UUID | None,
        state: str | None,
        template_path: str | None,
        status: str,
        status_reason: str | None,
        details: dict[str, Any] | None,
        recipient_email: str | None,
        rendered_by: str | None,
        rendered_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DisclosureRenderEventRecord:
        norm_state = _normalize_state(state)
        norm_status = _validate_status(status)
        ts = rendered_at or datetime.now(UTC)

        payload: dict[str, Any] = {
            "deal_id": str(deal_id) if deal_id is not None else None,
            "merchant_id": str(merchant_id) if merchant_id is not None else None,
            "state": norm_state,
            "template_path": template_path,
            "status": norm_status,
            "status_reason": status_reason,
            "details": details,
            "recipient_email": recipient_email,
            "rendered_at": ts.isoformat(),
            "rendered_by": rendered_by,
            "metadata": metadata,
        }

        try:
            result = (
                get_supabase()
                .table("disclosure_render_events")
                .insert(payload)
                .execute()
            )
        except Exception as exc:
            _log.error(
                "compliance.disclosure_render_event.write_failed "
                "status=%s state=%s",
                norm_status,
                norm_state,
            )
            raise DisclosureRenderEventWriteError(
                f"failed to record disclosure render event status={norm_status}"
            ) from exc

        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            raise DisclosureRenderEventWriteError(
                "supabase insert returned no row for disclosure render event"
            )
        return _row_to_record(rows[0])

    def list_by_status(
        self,
        *,
        status: str,
        limit: int = 50,
    ) -> list[DisclosureRenderEventRecord]:
        norm_status = _validate_status(status)
        try:
            result = (
                get_supabase()
                .table("disclosure_render_events")
                .select("*")
                .eq("status", norm_status)
                .order("rendered_at", desc=True)
                .limit(max(1, limit))
                .execute()
            )
        except Exception:
            _log.warning(
                "compliance.disclosure_render_event.list_by_status_failed "
                "status=%s",
                norm_status,
            )
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return [_row_to_record(r) for r in rows]

    def list_in_window(
        self,
        *,
        from_date: date,
        to_date: date,
        status: str | None = None,
        limit: int = 500,
    ) -> list[DisclosureRenderEventRecord]:
        if status is not None:
            _validate_status(status)
        # Inclusive window — Postgres TIMESTAMPTZ comparison against
        # ``to_date`` alone would exclude same-day rows after 00:00, so
        # the upper bound is end-of-day ISO. Mirrors the portfolio route.
        lower = datetime.combine(from_date, time.min, tzinfo=UTC).isoformat()
        upper = datetime.combine(to_date, time.max, tzinfo=UTC).isoformat()
        try:
            builder = (
                get_supabase()
                .table("disclosure_render_events")
                .select("*")
                .gte("rendered_at", lower)
                .lte("rendered_at", upper)
                .order("rendered_at", desc=True)
                .limit(max(1, limit))
            )
            if status is not None:
                builder = builder.eq("status", status)
            result = builder.execute()
        except Exception:
            _log.warning(
                "compliance.disclosure_render_event.list_in_window_failed "
                "from=%s to=%s status=%s",
                from_date,
                to_date,
                status,
            )
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return [_row_to_record(r) for r in rows]

    def get(self, event_id: UUID) -> DisclosureRenderEventRecord | None:
        try:
            result = (
                get_supabase()
                .table("disclosure_render_events")
                .select("*")
                .eq("id", str(event_id))
                .limit(1)
                .execute()
            )
        except Exception:
            _log.warning(
                "compliance.disclosure_render_event.get_failed id=%s",
                event_id,
            )
            return None
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            return None
        return _row_to_record(rows[0])


def _row_to_record(row: dict[str, Any]) -> DisclosureRenderEventRecord:
    """Map a Supabase row dict back into the Pydantic record."""
    rendered_at_raw = row["rendered_at"]
    rendered_at = (
        rendered_at_raw
        if isinstance(rendered_at_raw, datetime)
        else datetime.fromisoformat(str(rendered_at_raw).replace("Z", "+00:00"))
    )
    return DisclosureRenderEventRecord(
        id=UUID(row["id"]),
        deal_id=UUID(row["deal_id"]) if row.get("deal_id") else None,
        merchant_id=UUID(row["merchant_id"]) if row.get("merchant_id") else None,
        state=row.get("state"),
        template_path=row.get("template_path"),
        status=row["status"],
        status_reason=row.get("status_reason"),
        details=row.get("details"),
        recipient_email=row.get("recipient_email"),
        rendered_at=rendered_at,
        rendered_by=row.get("rendered_by"),
        metadata=row.get("metadata"),
    )


def record_disclosure_render_event(
    repo: DisclosureRenderEventRepository,
    audit: AuditLog,
    *,
    deal_id: UUID | None,
    merchant_id: UUID | None,
    state: str | None,
    template_path: str | None,
    status: str,
    status_reason: str | None,
    details: dict[str, Any] | None,
    recipient_email: str | None,
    rendered_by: str | None,
    actor: str = "api",
    actor_email: str | None = None,
    rendered_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> DisclosureRenderEventRecord:
    """Write a render-event row AND a paired ``audit_log`` row.

    Per CLAUDE.md "audit-write failures FAIL the operation" — the
    audit-log write must succeed. The render-event row is written FIRST
    so a render-event write failure surfaces before the audit row
    commits; if the audit_log write subsequently raises, the caller
    sees the audit failure and aborts (matching the rest of the
    compliance write surface).

    ``details`` is forwarded verbatim to BOTH writes. The audit log's
    own ``_mask_value`` pass scrubs PII patterns from the audit row;
    the render-event ``details`` JSONB is the caller's responsibility
    to keep PII-free (the route layer enforces this — see
    ``api/routes/disclosures.py``).
    """
    record = repo.record(
        deal_id=deal_id,
        merchant_id=merchant_id,
        state=state,
        template_path=template_path,
        status=status,
        status_reason=status_reason,
        details=details,
        recipient_email=recipient_email,
        rendered_by=rendered_by,
        rendered_at=rendered_at,
        metadata=metadata,
    )
    # Pair the render-event row with an audit_log entry so the durable
    # audit trail captures the event regardless of which side a reader
    # queries from. Subject is the deal when known; the route already
    # writes a separate ``aegis_apr_compute_failed`` audit row on the
    # APR failure path — this is an additional, structured render-event
    # audit signal, not a replacement.
    audit_details: dict[str, Any] = {
        "render_event_id": str(record.id),
        "status": record.status,
        "status_reason": record.status_reason,
        "state": record.state,
        "template_path": record.template_path,
        "details": details,
    }
    audit.record(
        actor=actor,
        actor_email=actor_email,
        action="aegis_disclosure_render_event",
        subject_type="deal",
        subject_id=deal_id,
        details=audit_details,
    )
    return record


__all__ = [
    "RENDER_EVENT_STATUS_APR_FAILED",
    "RENDER_EVENT_STATUS_NEEDS_REVIEW",
    "RENDER_EVENT_STATUS_OK",
    "RENDER_EVENT_STATUS_TEMPLATE_FAILED",
    "DisclosureRenderEventRecord",
    "DisclosureRenderEventRepository",
    "DisclosureRenderEventWriteError",
    "InMemoryDisclosureRenderEventRepository",
    "SupabaseDisclosureRenderEventRepository",
    "record_disclosure_render_event",
]
