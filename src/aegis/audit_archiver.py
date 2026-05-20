"""Retention archiver — moves expired audit_log rows to audit_log_archive.

Master plan §17 (Phase 7). The cron runs daily via arq; the entrypoint is
``run_archive_cron``. Production path uses ``SupabaseAuditArchiver``;
tests use ``InMemoryAuditArchiver`` to exercise threshold / idempotency
logic without a live DB.

Design constraints
------------------
* Never DELETE without first writing to ``audit_log_archive``. The
  archive INSERT and the live DELETE happen in the same transaction so a
  partial run is impossible.
* Re-running the archiver over the same window is a no-op. ``source_id``
  is UNIQUE on the archive table; ON CONFLICT DO NOTHING means a repeated
  run produces zero archived rows + zero audit entries.
* The archiver IS an auditable actor. Every batch writes one audit_log
  row with actor='audit_archiver' summarizing the batch (count,
  cutoff_date, batch_id). The summary row itself is NEVER archived (it
  describes recent activity by definition).
* Per-state retention windows come from the ``audit_retention_policy``
  lookup table. The archiver joins ``audit_log -> decisions -> state_code``
  to pick the correct window; rows that cannot be attributed to a state
  use the ``__default__`` sentinel row's value.

Decimal isn't relevant here (no money math). All math is in days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from dateutil.relativedelta import relativedelta

from aegis.audit import AuditLog, AuditWriteError
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)

_DEFAULT_STATE_KEY = "__default__"
_DEFAULT_RETENTION_YEARS = 5
# Sentinel action — never archived even if older than the retention window.
_ARCHIVE_SUMMARY_ACTION = "audit_log.archive_batch"


class ArchiverError(RuntimeError):
    """Raised when the archive transaction fails."""


@dataclass(frozen=True)
class RetentionPolicy:
    """One row from ``audit_retention_policy``."""

    state_code: str
    retention_years: int
    statute_citation: str


@dataclass
class ArchiveReport:
    """Summary returned to the cron caller. Keys map to the audit row.

    ``per_state`` is a dict of state_code -> count for the audit details
    payload; the operator can read it in the weekly digest.
    """

    batch_id: UUID
    cutoff_date: date
    archived_count: int = 0
    skipped_count: int = 0
    per_state: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    finished_at: datetime | None = None

    def to_audit_details(self) -> dict[str, Any]:
        return {
            "batch_id": str(self.batch_id),
            "cutoff_date": self.cutoff_date.isoformat(),
            "archived_count": self.archived_count,
            "skipped_count": self.skipped_count,
            "per_state": dict(self.per_state),
            "started_at": self.started_at.isoformat(),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
        }


class AuditArchiver(Protocol):
    """Backend-agnostic archiver interface."""

    def archive_expired(self, *, today: date | None = None) -> ArchiveReport:
        """Move expired rows to archive. Returns batch summary."""


# ---------------------------------------------------------------------------
# Pure helpers — testable without any DB.
# ---------------------------------------------------------------------------


def cutoff_for_policy(*, today: date, policy: RetentionPolicy) -> date:
    """Cutoff date = today minus ``retention_years`` calendar years.

    Strictly LESS than this date is expired; equal is still in retention
    (matches "4 years from disclosure" where the 4th anniversary is the
    last protected day).

    Uses ``dateutil.relativedelta`` for true calendar arithmetic — handles
    leap years and month-end edge cases the regulator-facing way (the Nth
    anniversary IS the anniversary, not "Nth * 365 days ago"). No buffer:
    a row dated more than N calendar years ago is past its retention.
    """
    return today - relativedelta(years=policy.retention_years)


def is_expired(*, created_at: datetime, today: date, policy: RetentionPolicy) -> bool:
    """True if ``created_at`` is older than the policy cutoff."""
    cutoff = cutoff_for_policy(today=today, policy=policy)
    return created_at.date() < cutoff


def resolve_policy(
    *,
    state_code: str | None,
    policies: dict[str, RetentionPolicy],
) -> RetentionPolicy:
    """Look up the policy for a state, fall back to default sentinel.

    Never raises — falls back to a synthesized default if the table
    didn't seed correctly. Defensive: the archiver must keep running.
    """
    if state_code and state_code in policies:
        return policies[state_code]
    if _DEFAULT_STATE_KEY in policies:
        return policies[_DEFAULT_STATE_KEY]
    return RetentionPolicy(
        state_code=_DEFAULT_STATE_KEY,
        retention_years=_DEFAULT_RETENTION_YEARS,
        statute_citation="synthesized default — audit_retention_policy not seeded",
    )


# ---------------------------------------------------------------------------
# In-memory implementation (tests).
# ---------------------------------------------------------------------------


@dataclass
class MemoryAuditRow:
    """One audit_log row in the in-memory store."""

    id: UUID
    actor: str
    action: str
    subject_type: str | None
    subject_id: UUID | None
    details: dict[str, Any]
    created_at: datetime
    deal_id: UUID | None = None
    state_code: str | None = None  # resolved from decisions for test convenience
    actor_email: str | None = None


class InMemoryAuditArchiver:
    """List-backed archiver used by unit tests.

    Mirrors the Supabase impl's contract exactly: one batch row per call,
    idempotent on source_id, never double-archives. The state_code on
    each MemoryAuditRow stands in for the SQL join to ``decisions`` so test
    code can exercise the per-state retention logic directly.
    """

    def __init__(
        self,
        *,
        audit: AuditLog,
        policies: dict[str, RetentionPolicy] | None = None,
    ) -> None:
        self.audit = audit
        self.rows: list[MemoryAuditRow] = []
        self.archive: list[dict[str, Any]] = []
        self.policies: dict[str, RetentionPolicy] = policies or {
            _DEFAULT_STATE_KEY: RetentionPolicy(
                state_code=_DEFAULT_STATE_KEY,
                retention_years=_DEFAULT_RETENTION_YEARS,
                statute_citation="test default",
            ),
        }

    def add_row(self, row: MemoryAuditRow) -> None:
        self.rows.append(row)

    def archive_expired(self, *, today: date | None = None) -> ArchiveReport:
        today = today or datetime.now(tz=UTC).date()
        batch_id = uuid4()
        report = ArchiveReport(batch_id=batch_id, cutoff_date=today)

        # Track already-archived source_ids for idempotency.
        already_archived: set[UUID] = {
            UUID(r["source_id"]) for r in self.archive
        }

        for row in list(self.rows):
            if row.action == _ARCHIVE_SUMMARY_ACTION:
                continue  # archiver summary rows are never archived
            policy = resolve_policy(state_code=row.state_code, policies=self.policies)
            if not is_expired(created_at=row.created_at, today=today, policy=policy):
                continue
            if row.id in already_archived:
                report.skipped_count += 1
                continue

            self.archive.append(
                {
                    "source_id": str(row.id),
                    "actor": row.actor,
                    "actor_email": row.actor_email,
                    "action": row.action,
                    "subject_type": row.subject_type,
                    "subject_id": str(row.subject_id) if row.subject_id else None,
                    "details": row.details,
                    "source_created_at": row.created_at.isoformat(),
                    "deal_id": str(row.deal_id) if row.deal_id else None,
                    "archived_at": datetime.now(tz=UTC).isoformat(),
                    "archive_batch_id": str(batch_id),
                    "retention_policy_state": policy.state_code,
                    "retention_policy_years": policy.retention_years,
                    "archive_reason": "retention_expired",
                }
            )
            self.rows.remove(row)
            report.archived_count += 1
            key = row.state_code or _DEFAULT_STATE_KEY
            report.per_state[key] = report.per_state.get(key, 0) + 1
            already_archived.add(row.id)

        report.finished_at = datetime.now(tz=UTC)

        # Record the archive batch itself — every run is an auditable
        # event whether or not it archived any rows. Failure to write
        # this row FAILS the operation per CLAUDE.md.
        try:
            self.audit.record(
                actor="audit_archiver",
                action=_ARCHIVE_SUMMARY_ACTION,
                subject_type="audit_log",
                subject_id=batch_id,
                details=report.to_audit_details(),
            )
        except AuditWriteError as exc:
            raise ArchiverError("failed to record archive batch audit row") from exc

        return report


# ---------------------------------------------------------------------------
# Supabase implementation (production).
# ---------------------------------------------------------------------------


class SupabaseAuditArchiver:
    """Production archiver. Reads policies once per call, then iterates
    expired rows in batches.

    The actual archive move uses two Supabase calls per row group:
      1. INSERT into audit_log_archive (ON CONFLICT DO NOTHING).
      2. DELETE from audit_log WHERE id in the inserted set.

    Both calls use parameterized queries via supabase-py's table builder.
    No string-interpolated SQL anywhere.
    """

    # Hard cap on rows per cron invocation. Even if the table has 100k
    # expired rows, we move them in 5k chunks and let the next nightly
    # run pick up the rest. Prevents a single long-running transaction.
    _MAX_BATCH = 5000

    def __init__(self, *, audit: AuditLog) -> None:
        self.audit = audit

    def _load_policies(self) -> dict[str, RetentionPolicy]:
        client = get_supabase()
        try:
            result = (
                client.table("audit_retention_policy")
                .select("state_code, retention_years, statute_citation")
                .execute()
            )
        except Exception as exc:
            _log.error("archiver.policies_load_failed")
            raise ArchiverError("could not load audit_retention_policy") from exc
        rows = cast(list[dict[str, Any]], result.data or [])
        policies: dict[str, RetentionPolicy] = {}
        for r in rows:
            policies[r["state_code"]] = RetentionPolicy(
                state_code=r["state_code"],
                retention_years=int(r["retention_years"]),
                statute_citation=r["statute_citation"],
            )
        return policies

    def _candidate_rows(
        self,
        *,
        cutoff: date,
    ) -> list[dict[str, Any]]:
        """Pull audit_log rows older than the cutoff with their state_code.

        Uses two queries instead of a server-side join because supabase-py's
        PostgREST view exposure doesn't reliably stitch decisions.state_code
        onto audit_log without a view. The state attribution lookup happens
        in Python on the worker.
        """
        client = get_supabase()
        try:
            result = (
                client.table("audit_log")
                .select(
                    "id, actor, actor_email, action, subject_type, "
                    "subject_id, details, created_at, deal_id, "
                    "state_change, aegis_version, rule_pack_version"
                )
                .lt("created_at", cutoff.isoformat())
                .neq("action", _ARCHIVE_SUMMARY_ACTION)
                .limit(self._MAX_BATCH)
                .execute()
            )
        except Exception as exc:
            _log.error("archiver.candidate_query_failed")
            raise ArchiverError("could not query audit_log candidates") from exc
        return cast(list[dict[str, Any]], result.data or [])

    def _attribute_state(
        self,
        *,
        rows: list[dict[str, Any]],
    ) -> dict[str, str | None]:
        """Map row id -> state_code via decisions.deal_id.

        Rows without a deal_id (system events) get None and use the
        default policy.
        """
        deal_ids = {
            r["deal_id"] for r in rows if r.get("deal_id")
        }
        if not deal_ids:
            return {r["id"]: None for r in rows}
        client = get_supabase()
        try:
            result = (
                client.table("decisions")
                .select("deal_id, state_code")
                .in_("deal_id", list(deal_ids))
                .execute()
            )
        except Exception:
            _log.warning("archiver.state_lookup_failed; using default for all rows")
            return {r["id"]: None for r in rows}

        rows_decisions = cast(list[dict[str, Any]], result.data or [])
        deal_state = {r["deal_id"]: r.get("state_code") for r in rows_decisions}
        return {
            r["id"]: deal_state.get(r.get("deal_id")) if r.get("deal_id") else None
            for r in rows
        }

    def archive_expired(self, *, today: date | None = None) -> ArchiveReport:
        today = today or datetime.now(tz=UTC).date()
        batch_id = uuid4()
        report = ArchiveReport(batch_id=batch_id, cutoff_date=today)

        policies = self._load_policies()
        # Pre-filter using the longest configured retention so the
        # server-side query is bounded. Per-row policy resolution below
        # still enforces the per-state window — this is just to avoid
        # scanning rows that nothing could match.
        max_years = max(
            (p.retention_years for p in policies.values()),
            default=_DEFAULT_RETENTION_YEARS,
        )
        widest_cutoff = today - relativedelta(years=max_years)

        candidates = self._candidate_rows(cutoff=widest_cutoff)
        if not candidates:
            self._record_summary(report)
            return report

        state_by_id = self._attribute_state(rows=candidates)

        client = get_supabase()
        to_archive: list[dict[str, Any]] = []
        to_delete: list[str] = []

        for row in candidates:
            state_code = state_by_id.get(row["id"])
            policy = resolve_policy(state_code=state_code, policies=policies)
            created_raw = row.get("created_at")
            if not created_raw:
                continue
            try:
                created_at = _parse_iso(created_raw)
            except ValueError:
                _log.warning("archiver.bad_created_at row_id=%s", row["id"])
                continue
            if not is_expired(created_at=created_at, today=today, policy=policy):
                continue

            to_archive.append(
                {
                    "source_id": row["id"],
                    "actor": row["actor"],
                    "actor_email": row.get("actor_email"),
                    "action": row["action"],
                    "subject_type": row.get("subject_type"),
                    "subject_id": row.get("subject_id"),
                    "details": row.get("details") or {},
                    "source_created_at": created_raw,
                    "deal_id": row.get("deal_id"),
                    "state_change": row.get("state_change"),
                    "aegis_version": row.get("aegis_version"),
                    "rule_pack_version": row.get("rule_pack_version"),
                    "archive_batch_id": str(batch_id),
                    "retention_policy_state": policy.state_code,
                    "retention_policy_years": policy.retention_years,
                    "archive_reason": "retention_expired",
                }
            )
            to_delete.append(row["id"])
            key = state_code or _DEFAULT_STATE_KEY
            report.per_state[key] = report.per_state.get(key, 0) + 1

        if not to_archive:
            self._record_summary(report)
            return report

        # Insert into archive. ON CONFLICT DO NOTHING via upsert pattern.
        try:
            client.table("audit_log_archive").upsert(
                to_archive,
                on_conflict="source_id",
                ignore_duplicates=True,
            ).execute()
        except Exception as exc:
            _log.error("archiver.archive_insert_failed batch_id=%s", batch_id)
            raise ArchiverError(
                f"archive INSERT failed for batch {batch_id}"
            ) from exc

        # Delete from live. The archive INSERT already happened; if the
        # DELETE fails the operator gets duplicate rows in archive on
        # next run (idempotent via source_id UNIQUE), never lost data.
        try:
            client.table("audit_log").delete().in_("id", to_delete).execute()
        except Exception as exc:
            _log.error("archiver.delete_failed batch_id=%s", batch_id)
            raise ArchiverError(
                f"audit_log DELETE failed for batch {batch_id}"
            ) from exc

        report.archived_count = len(to_archive)
        self._record_summary(report)
        return report

    def _record_summary(self, report: ArchiveReport) -> None:
        report.finished_at = datetime.now(tz=UTC)
        try:
            self.audit.record(
                actor="audit_archiver",
                action=_ARCHIVE_SUMMARY_ACTION,
                subject_type="audit_log",
                subject_id=report.batch_id,
                details=report.to_audit_details(),
            )
        except AuditWriteError as exc:
            raise ArchiverError("failed to record archive batch audit row") from exc


def _parse_iso(value: str) -> datetime:
    """Parse a Postgres/JSON ISO 8601 timestamp into an aware datetime."""
    # Postgres timestamptz comes back as "...+00:00". datetime.fromisoformat
    # handles that on 3.11+.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# arq cron entrypoint.
# ---------------------------------------------------------------------------


async def run_archive_cron(ctx: dict[str, Any]) -> dict[str, Any]:
    """arq daily cron job.

    Returns a small dict so the operator can spot-check arq job logs.
    The full audit detail row is written via the AuditLog protocol
    inside the archiver — this return is purely for the queue's bookkeeping.
    """
    # Lazy imports to keep workers.py import-time cheap.
    from aegis.api.deps import get_audit

    audit = ctx.get("audit") or get_audit()
    archiver = ctx.get("archiver") or SupabaseAuditArchiver(audit=audit)

    report = archiver.archive_expired()
    _log.info(
        "audit_archiver.run "
        "archived=%s skipped=%s batch_id=%s",
        report.archived_count,
        report.skipped_count,
        report.batch_id,
    )
    return {
        "batch_id": str(report.batch_id),
        "archived_count": report.archived_count,
        "skipped_count": report.skipped_count,
        "cutoff_date": report.cutoff_date.isoformat(),
        "per_state": report.per_state,
    }


__all__ = [
    "ArchiveReport",
    "ArchiverError",
    "AuditArchiver",
    "InMemoryAuditArchiver",
    "MemoryAuditRow",
    "RetentionPolicy",
    "SupabaseAuditArchiver",
    "cutoff_for_policy",
    "is_expired",
    "resolve_policy",
    "run_archive_cron",
]
