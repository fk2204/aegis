"""Compliance obligations reader + tracker (master plan §17 / §9.5).

Two surfaces live in this module:

  * ``ObligationsRepository`` — read-only annotator that powers the
    ``/ui/compliance/obligations`` dashboard. Reads from
    ``compliance_obligations`` (migration 018 / 069) and returns a
    typed list of obligations annotated with derived state
    ('overdue', 'due_soon', 'on_track').

  * ``ComplianceObligationRepository`` — mutation-aware tracker that
    powers the Today-dashboard "Compliance deadlines" attention card
    AND the weekly arq cron that fires reminder audit rows at the
    60 / 30 / 14-day thresholds before ``next_due_date``. Adds
    ``list_upcoming(days)`` and ``mark_status(id, status)``.

Both backends mirror each other so dashboard + cron tests can exercise
every path without a live DB.

Status transitions go through ``mark_status``, which:

  1. UPDATEs the row,
  2. writes one ``compliance.obligation_status_changed`` audit row, and
  3. propagates audit-write failures up so the calling operation
     fails rather than silently log-and-continue (CLAUDE.md compliance
     non-negotiable: every state change writes an audit row, failures
     fail the operation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.audit import AuditLog
from aegis.config import get_settings
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)

# Reminder thresholds (days before next_due_date) for the weekly cron.
# Operator-facing dashboard card buckets these into red / amber / yellow.
REMINDER_THRESHOLD_DAYS: tuple[int, ...] = (60, 30, 14)

# Today dashboard "Compliance deadlines" attention-card horizon.
# Widened in two passes:
#   * 90 → 180 days (2026-06-28) so the TX OCCC 2026-12-31 filing
#     (~186 days out at widen time) surfaces with operator lead time.
#   * 180 → 200 days (2026-06-30) — 180 turned out to be 4 days too
#     short the next time we measured (TX HB 700 = 184 days from
#     2026-06-30), so the card silently went empty. 200 gives a small
#     buffer against the same boundary problem ticking around midyear
#     turn-overs. Reminder cron thresholds (REMINDER_THRESHOLD_DAYS)
#     are NOT touched — they remain 60/30/14 days for audit-row firing.
TODAY_CARD_HORIZON_DAYS: int = 200

# Color buckets for the Today card. Matches REMINDER_THRESHOLD_DAYS so
# an item that just fired a 14-day reminder shows red; a 30-day fire
# shows amber; a 60-day fire shows yellow. Inclusive lower bound.
URGENCY_RED_MAX_DAYS: int = 14
URGENCY_AMBER_MAX_DAYS: int = 30
URGENCY_YELLOW_MAX_DAYS: int = 60

# Days inside which a "due_soon" pill renders. Anything past today is
# 'overdue'; anything beyond this horizon is 'on_track'. 60 days is the
# operator's standard prep cycle for a state filing.
_DUE_SOON_HORIZON_DAYS = 60

ObligationStatus = Literal["not_started", "in_progress", "submitted", "active", "lapsed"]
DerivedState = Literal["overdue", "due_soon", "on_track", "no_deadline"]


class ObligationRow(BaseModel):
    """One row from ``compliance_obligations`` with derived display state.

    The DB row may carry additional columns from a future migration;
    ``extra="ignore"`` keeps this readable forward-compat (same pattern
    as the audit route).
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str
    obligation_type: str
    state_code: str
    authority: str
    description: str
    deadline: date | None = None
    recurrence: str | None = None
    status: ObligationStatus
    next_due_date: date | None = None
    evidence_file_path: str | None = None
    last_reviewed: str | None = None
    notes: str | None = None

    # Derived — not a DB column.
    derived_state: DerivedState = Field(default="no_deadline")
    days_until_due: int | None = None


@dataclass(frozen=True)
class ObligationsSummary:
    """Aggregate counts for the dashboard tile."""

    total: int
    overdue: int
    due_soon: int
    on_track: int
    submitted_or_active: int


class ObligationsRepository(Protocol):
    """Backend-agnostic reader."""

    def list_obligations(self) -> list[ObligationRow]: ...


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class InMemoryObligationsRepository:
    """Test-side repository. Operator-supplied rows + the derive step."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])

    def list_obligations(self) -> list[ObligationRow]:
        return _annotate_rows(self.rows, today=date.today())


class SupabaseObligationsRepository:
    """Production reader. Reads compliance_obligations and annotates."""

    def list_obligations(self) -> list[ObligationRow]:
        try:
            result = (
                get_supabase()
                .table("compliance_obligations")
                .select("*")
                .order("next_due_date", desc=False)
                .execute()
            )
        except Exception:
            _log.warning("obligations.list_failed")
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return _annotate_rows(rows, today=date.today())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _annotate_rows(rows: list[dict[str, Any]], *, today: date) -> list[ObligationRow]:
    annotated: list[ObligationRow] = []
    for raw in rows:
        try:
            row = ObligationRow.model_validate(raw)
        except Exception:
            _log.warning("obligations.bad_row id=%s", raw.get("id"))
            continue
        annotated.append(_apply_derived_state(row, today=today))
    # Sort by derived urgency: overdue -> due_soon -> on_track -> no_deadline.
    # Within a bucket, by next_due_date ascending (None last).
    order_key = {"overdue": 0, "due_soon": 1, "on_track": 2, "no_deadline": 3}
    return sorted(
        annotated,
        key=lambda r: (
            order_key[r.derived_state],
            r.next_due_date or date.max,
            r.deadline or date.max,
            r.state_code,
        ),
    )


def _apply_derived_state(row: ObligationRow, *, today: date) -> ObligationRow:
    """Set derived_state + days_until_due on the row.

    Uses the earliest of next_due_date or deadline. Submitted/active
    statuses suppress overdue/due_soon (the obligation is met until the
    next recurrence rolls).
    """
    target = _earliest_deadline(row)
    if target is None:
        return row.model_copy(update={"derived_state": "no_deadline"})

    delta = (target - today).days

    if row.status in {"submitted", "active"}:
        return row.model_copy(
            update={
                "derived_state": "on_track",
                "days_until_due": delta,
            }
        )

    if delta < 0:
        derived: DerivedState = "overdue"
    elif delta <= _DUE_SOON_HORIZON_DAYS:
        derived = "due_soon"
    else:
        derived = "on_track"

    return row.model_copy(
        update={
            "derived_state": derived,
            "days_until_due": delta,
        }
    )


def _earliest_deadline(row: ObligationRow) -> date | None:
    candidates = [d for d in (row.next_due_date, row.deadline) if d is not None]
    return min(candidates) if candidates else None


def summarize(rows: list[ObligationRow]) -> ObligationsSummary:
    """Aggregate counts used by the dashboard header tiles."""
    overdue = sum(1 for r in rows if r.derived_state == "overdue")
    due_soon = sum(1 for r in rows if r.derived_state == "due_soon")
    on_track = sum(1 for r in rows if r.derived_state == "on_track")
    submitted_or_active = sum(1 for r in rows if r.status in {"submitted", "active"})
    return ObligationsSummary(
        total=len(rows),
        overdue=overdue,
        due_soon=due_soon,
        on_track=on_track,
        submitted_or_active=submitted_or_active,
    )


def get_obligations_repository() -> ObligationsRepository:
    """Backend selector. Mirrors the rest of api.deps."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryObligationsRepository()
    return SupabaseObligationsRepository()


# ---------------------------------------------------------------------------
# Compliance obligation tracker — adds list_upcoming + mark_status on top
# of the read-only annotator above. Keeps the existing dashboard surface
# unchanged while the Today-card + weekly cron consume the new API.
# ---------------------------------------------------------------------------


UrgencyBucket = Literal["red", "amber", "yellow"]


@dataclass(frozen=True)
class UpcomingObligation:
    """One row surfaced by ``list_upcoming``.

    ``days_remaining`` is the calendar-day count between today and
    ``next_due_date`` — negative for overdue items. The Today card
    uses ``urgency`` (red / amber / yellow); the cron consumes
    ``days_remaining`` directly to decide which threshold (60/30/14)
    just fired.
    """

    id: UUID
    state_code: str
    authority: str
    description: str
    status: ObligationStatus
    next_due_date: date
    days_remaining: int
    urgency: UrgencyBucket


class ComplianceObligationRepository(Protocol):
    """Backend-agnostic tracker that the Today card + weekly cron call.

    Distinct from ``ObligationsRepository`` above: that one is the
    legacy read-only annotator for the full /ui/compliance/obligations
    page. This one returns only ``next_due_date``-bearing rows within
    a window AND mutates state, so it has different write-failure
    semantics (audit-write failures FAIL the operation).
    """

    def list_upcoming(
        self, days: int, *, today: date | None = None
    ) -> list[UpcomingObligation]: ...

    def mark_status(
        self,
        obligation_id: UUID,
        status: ObligationStatus,
        *,
        audit: AuditLog,
        actor: str = "operator",
    ) -> None: ...


class InMemoryComplianceObligationRepository:
    """List-backed tracker for tests + the memory storage backend.

    Mutates ``rows`` in place on ``mark_status``. The InMemoryAuditLog
    + this repo share the test session so audit assertions can read
    ``audit.entries`` directly after mark_status returns.
    """

    def __init__(self, *, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = list(rows or [])

    def list_upcoming(
        self,
        days: int,
        *,
        today: date | None = None,
    ) -> list[UpcomingObligation]:
        today = today or datetime.now(UTC).date()
        return _build_upcoming(self.rows, days=days, today=today)

    def mark_status(
        self,
        obligation_id: UUID,
        status: ObligationStatus,
        *,
        audit: AuditLog,
        actor: str = "operator",
    ) -> None:
        target_id = str(obligation_id)
        for row in self.rows:
            if row.get("id") == target_id:
                prior_status = row.get("status")
                row["status"] = status
                # Audit BEFORE returning: failure propagates so the
                # operation rolls back conceptually (the in-memory write
                # already mutated, but the audit-fail surface to the
                # caller is identical to Supabase's semantics).
                audit.record(
                    actor=actor,
                    action="compliance.obligation_status_changed",
                    subject_type="compliance_obligation",
                    subject_id=obligation_id,
                    details={
                        "from_status": prior_status,
                        "to_status": status,
                        "state_code": row.get("state_code"),
                        "authority": row.get("authority"),
                    },
                )
                return
        raise ObligationNotFoundError(f"compliance_obligation id={obligation_id} not found")


class SupabaseComplianceObligationRepository:
    """Production tracker. Reads + writes ``compliance_obligations``."""

    def list_upcoming(self, days: int, *, today: date | None = None) -> list[UpcomingObligation]:
        today = today or datetime.now(UTC).date()
        cutoff = today + timedelta(days=max(0, days))
        try:
            result = (
                get_supabase()
                .table("compliance_obligations")
                .select("id,state_code,authority,description,status,next_due_date")
                .not_.is_("next_due_date", "null")
                .lte("next_due_date", cutoff.isoformat())
                .order("next_due_date", desc=False)
                .execute()
            )
        except Exception:
            _log.warning("compliance.list_upcoming_failed")
            return []
        rows = cast(list[dict[str, Any]], result.data or [])
        return _build_upcoming(rows, days=days, today=today)

    def mark_status(
        self,
        obligation_id: UUID,
        status: ObligationStatus,
        *,
        audit: AuditLog,
        actor: str = "operator",
    ) -> None:
        # Fetch the prior row first so the audit detail captures the
        # transition correctly (Supabase update().execute() doesn't
        # surface the pre-update value on every server version).
        try:
            prior_result = (
                get_supabase()
                .table("compliance_obligations")
                .select("status,state_code,authority")
                .eq("id", str(obligation_id))
                .limit(1)
                .execute()
            )
        except Exception as exc:
            raise ObligationNotFoundError(
                f"compliance_obligation id={obligation_id} lookup failed"
            ) from exc
        prior_rows = cast(list[dict[str, Any]], prior_result.data or [])
        if not prior_rows:
            raise ObligationNotFoundError(f"compliance_obligation id={obligation_id} not found")
        prior = prior_rows[0]

        get_supabase().table("compliance_obligations").update(
            {
                "status": status,
                "last_reviewed": datetime.now(UTC).isoformat(),
            }
        ).eq("id", str(obligation_id)).execute()

        # Audit-write failure propagates per CLAUDE.md "never silently
        # log-and-continue". SupabaseAuditLog raises AuditWriteError on
        # failure; the calling operation aborts.
        audit.record(
            actor=actor,
            action="compliance.obligation_status_changed",
            subject_type="compliance_obligation",
            subject_id=obligation_id,
            details={
                "from_status": prior.get("status"),
                "to_status": status,
                "state_code": prior.get("state_code"),
                "authority": prior.get("authority"),
            },
        )


class ObligationNotFoundError(LookupError):
    """Raised when ``mark_status`` targets a row that doesn't exist."""


def _build_upcoming(
    rows: list[dict[str, Any]],
    *,
    days: int,
    today: date,
) -> list[UpcomingObligation]:
    """Pure shaping helper shared by both backends.

    Filters to rows whose ``next_due_date`` is present AND within
    ``[today, today+days]`` inclusive. Sorts ascending by urgency
    (earliest deadline first). Rows lacking next_due_date are skipped
    silently — there's nothing to fire a reminder against.

    Past-due rows (``days_remaining < 0``) are included; the operator
    needs to see them on the Today card even after the deadline passes.
    """
    out: list[UpcomingObligation] = []
    horizon = today + timedelta(days=max(0, days))
    for raw in rows:
        next_due = _parse_date(raw.get("next_due_date"))
        if next_due is None or next_due > horizon:
            continue
        try:
            obligation_id = UUID(str(raw.get("id")))
        except (TypeError, ValueError):
            _log.warning("compliance.bad_obligation_id id=%s", raw.get("id"))
            continue
        status_raw = raw.get("status") or "not_started"
        if status_raw not in {
            "not_started",
            "in_progress",
            "submitted",
            "active",
            "lapsed",
        }:
            _log.warning(
                "compliance.bad_status id=%s status=%s",
                obligation_id,
                status_raw,
            )
            continue
        days_remaining = (next_due - today).days
        out.append(
            UpcomingObligation(
                id=obligation_id,
                state_code=str(raw.get("state_code") or ""),
                authority=str(raw.get("authority") or ""),
                description=str(raw.get("description") or ""),
                status=cast(ObligationStatus, status_raw),
                next_due_date=next_due,
                days_remaining=days_remaining,
                urgency=_classify_urgency(days_remaining),
            )
        )
    # Earliest deadline first — surfaces the most-urgent item at top.
    out.sort(key=lambda u: (u.next_due_date, u.state_code))
    return out


def _parse_date(value: Any) -> date | None:  # noqa: ANN401 — Supabase rows are dict[str, Any]; the field may arrive as date, str, or None depending on which backend supplied it.
    """Tolerant date parser — accepts ``date`` or ISO 'YYYY-MM-DD'."""
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _classify_urgency(days_remaining: int) -> UrgencyBucket:
    """Map calendar-day remaining → Today card color bucket.

    Negative (overdue) days fall into ``red`` so the operator sees the
    most-urgent items at the top of the Today card with the most-
    alarming color. The cron threshold-firing logic uses
    ``days_remaining`` directly, NOT this bucket.
    """
    if days_remaining <= URGENCY_RED_MAX_DAYS:
        return "red"
    if days_remaining <= URGENCY_AMBER_MAX_DAYS:
        return "amber"
    return "yellow"


def get_compliance_obligation_repository() -> ComplianceObligationRepository:
    """Backend selector for the tracker. Mirrors the other api.deps slots."""
    if get_settings().aegis_storage_backend == "memory":
        return InMemoryComplianceObligationRepository()
    return SupabaseComplianceObligationRepository()


# ---------------------------------------------------------------------------
# Weekly cron — fires reminder audit rows at the 60 / 30 / 14-day
# thresholds before next_due_date. Registered on WorkerSettings.cron_jobs
# at workers.py.
# ---------------------------------------------------------------------------


def _has_reminder_already_fired(
    audit: AuditLog,
    *,
    obligation_id: UUID,
    threshold: int,
    next_due_date: date,
) -> bool:
    """Per-(obligation, threshold, next_due_date) dedupe lookup.

    The cron runs weekly; a 60-day reminder fires once per
    (obligation, next_due_date, threshold=60) and never again until
    ``next_due_date`` rolls. Dedup key keys on all three so a
    next-cycle next_due_date (operator updated to the new annual
    anniversary) fires fresh reminders.

    The audit subject is the obligation, and the lookup is bounded by
    ``limit=200`` so a long-running obligation with many prior
    reminders still returns predictably.
    """
    rows = audit.list_for_subject(
        subject_type="compliance_obligation",
        subject_id=obligation_id,
        action="compliance.deadline_approaching",
        limit=200,
    )
    iso_due = next_due_date.isoformat()
    for r in rows:
        details = r.get("details") or {}
        if details.get("threshold_days") == threshold and details.get("next_due_date") == iso_due:
            return True
    return False


def run_compliance_obligation_reminder_pass(
    *,
    audit: AuditLog,
    obligations: ComplianceObligationRepository,
    today: date | None = None,
    thresholds: tuple[int, ...] = REMINDER_THRESHOLD_DAYS,
) -> dict[str, int]:
    """Fire ``compliance.deadline_approaching`` audit rows at threshold crossings.

    For every obligation whose ``next_due_date`` lands within the
    widest threshold (60 days, by default), check each threshold in
    descending order (60 → 30 → 14). A reminder fires when
    ``days_remaining <= threshold`` AND a prior reminder for that
    (obligation, threshold, next_due_date) tuple has NOT already been
    written. A single cron run can fire multiple thresholds for one
    obligation if the cron missed a window (e.g. weekly cadence misses
    a 60→30 crossover by a few days) — that's the operator-safe
    behavior.

    Excluded by design:

      * Rows whose ``next_due_date`` is NULL (no deadline → no
        reminder).
      * Rows whose ``status`` is ``submitted`` or ``active`` — the
        obligation is met; reminders for the current cycle would be
        noise. The operator transitions back to ``not_started`` when
        the next recurrence rolls.

    Returns a small summary dict so the cron wrapper can log
    spot-check counts. The authoritative trail is in ``audit_log``.
    """
    today = today or datetime.now(UTC).date()
    widest = max(thresholds) if thresholds else 0
    rows = obligations.list_upcoming(widest, today=today)

    considered = 0
    fired = 0
    skipped_already_fired = 0
    skipped_status_met = 0

    for row in rows:
        considered += 1
        if row.status in {"submitted", "active"}:
            skipped_status_met += 1
            continue

        for threshold in sorted(thresholds, reverse=True):
            if row.days_remaining > threshold:
                continue
            if _has_reminder_already_fired(
                audit,
                obligation_id=row.id,
                threshold=threshold,
                next_due_date=row.next_due_date,
            ):
                skipped_already_fired += 1
                continue
            audit.record(
                actor="cron.compliance_obligation_reminder",
                action="compliance.deadline_approaching",
                subject_type="compliance_obligation",
                subject_id=row.id,
                details={
                    # ``description`` is masked by aegis.logger as a PII key
                    # (transaction descriptions). The obligation description
                    # is not PII — it's a static regulatory label — so use a
                    # distinct key to bypass the mask.
                    "obligation_description": row.description,
                    "authority": row.authority,
                    "state_code": row.state_code,
                    "days_remaining": row.days_remaining,
                    "deadline": row.next_due_date.isoformat(),
                    "next_due_date": row.next_due_date.isoformat(),
                    "threshold_days": threshold,
                },
            )
            fired += 1

    return {
        "considered": considered,
        "fired": fired,
        "skipped_already_fired": skipped_already_fired,
        "skipped_status_met": skipped_status_met,
    }


async def run_compliance_obligation_reminder_cron(
    ctx: dict[str, Any],
) -> dict[str, int]:
    """arq weekly cron entrypoint — Mon 07:00 UTC.

    Pulls dependencies from the arq ctx (tests inject in-memory fakes)
    and falls back to process-wide DI when absent — same pattern as
    ``run_renewal_reminder_cron`` / ``run_submission_reminder_cron``.
    """
    from aegis.api.deps import get_audit

    audit = ctx.get("audit") or get_audit()
    obligations = ctx.get("obligations") or get_compliance_obligation_repository()

    summary = run_compliance_obligation_reminder_pass(
        audit=audit,
        obligations=obligations,
    )
    _log.info(
        "compliance_obligation_reminder.run considered=%s fired=%s "
        "skipped_already_fired=%s skipped_status_met=%s",
        summary["considered"],
        summary["fired"],
        summary["skipped_already_fired"],
        summary["skipped_status_met"],
    )
    return summary


# ---------------------------------------------------------------------------
# Today-dashboard "Compliance deadlines" attention card.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplianceCard:
    """Today-dashboard attention card payload.

    Mirrors the dict shape the existing stale-deals / awaiting-funder
    sections render — one anchor + one needs string per row — so the
    template iterates the same shape across all three attention
    sections.
    """

    obligation_id: str
    state_code: str
    authority: str
    description: str
    days_remaining: int
    urgency: UrgencyBucket
    href: str
    test_id: str
    needs: str


def build_compliance_attention_section(
    obligations: ComplianceObligationRepository,
    *,
    horizon_days: int = TODAY_CARD_HORIZON_DAYS,
    cap: int = 8,
) -> tuple[int, list[str], list[ComplianceCard]]:
    """Build the Today-dashboard "Compliance deadlines" attention card.

    Returns ``(count, source_obligation_ids, cards)`` paired per
    CLAUDE.md auditability rule — every aggregate carries its source
    IDs. ``count`` is the total within the horizon (not the cap);
    ``cards`` is capped to keep the right-rail render bounded when
    the operator has a long backlog. ``source_obligation_ids`` carries
    every contributing id so a future drill-down link is faithful.

    Color buckets:
      * red    — days_remaining <= 14 (or overdue)
      * amber  — 15..30 days
      * yellow — 31..60 days

    Rows beyond ``TODAY_CARD_HORIZON_DAYS=200`` are skipped — the
    operator's standard prep cycle is 60 days, and the 200-day
    horizon (widened from 180 on 2026-06-30, originally 90) gives
    long-lead state filings such as the TX OCCC 2026-12-31 deadline
    runway on the Today card before the 60-day reminder cron starts
    firing. Reminder cron thresholds are unchanged at 60/30/14 days.
    """
    rows = obligations.list_upcoming(horizon_days)
    source_ids: list[str] = [str(r.id) for r in rows]

    cards: list[ComplianceCard] = []
    for row in rows[:cap]:
        cards.append(
            ComplianceCard(
                obligation_id=str(row.id),
                state_code=row.state_code,
                authority=row.authority,
                description=row.description,
                days_remaining=row.days_remaining,
                urgency=row.urgency,
                href="/ui/compliance/obligations",
                test_id="today-attn-compliance",
                needs=_compliance_needs_string(row),
            )
        )
    return len(rows), source_ids, cards


def _compliance_needs_string(row: UpcomingObligation) -> str:
    """Short worker-readable "what do I need to do" string.

    Mirrors the prose style of the existing stale-deals card
    (``f"No new document in {n} days — chase the merchant"``):
    one sentence, action verb, no PII (compliance rows carry none
    anyway).
    """
    days = row.days_remaining
    if days < 0:
        return f"Overdue by {-days} day{'s' if -days != 1 else ''} — file with {row.authority}"
    if days == 0:
        return f"Due today — file with {row.authority}"
    return f"Due in {days} day{'s' if days != 1 else ''} — prepare {row.authority} filing"


__all__ = [
    "REMINDER_THRESHOLD_DAYS",
    "TODAY_CARD_HORIZON_DAYS",
    "URGENCY_AMBER_MAX_DAYS",
    "URGENCY_RED_MAX_DAYS",
    "URGENCY_YELLOW_MAX_DAYS",
    "ComplianceCard",
    "ComplianceObligationRepository",
    "DerivedState",
    "InMemoryComplianceObligationRepository",
    "InMemoryObligationsRepository",
    "ObligationNotFoundError",
    "ObligationRow",
    "ObligationStatus",
    "ObligationsRepository",
    "ObligationsSummary",
    "SupabaseComplianceObligationRepository",
    "SupabaseObligationsRepository",
    "UpcomingObligation",
    "UrgencyBucket",
    "build_compliance_attention_section",
    "get_compliance_obligation_repository",
    "get_obligations_repository",
    "run_compliance_obligation_reminder_cron",
    "run_compliance_obligation_reminder_pass",
    "summarize",
]
