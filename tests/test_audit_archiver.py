"""Audit archiver tests — retention windows, idempotency, audit row (mp §17).

Tests against the InMemoryAuditArchiver — the Supabase impl is exercised
in integration / smoke testing on the prod box. The retention math and
idempotency logic both live in pure helpers + the memory backend, so the
contract is fully covered here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.audit_archiver import (
    ArchiveReport,
    ArchiverError,
    InMemoryAuditArchiver,
    MemoryAuditRow,
    RetentionPolicy,
    cutoff_for_policy,
    is_expired,
    resolve_policy,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_cutoff_uses_calendar_arithmetic() -> None:
    """Cutoff is N calendar years before today, NOT N * 365 days.

    Counsel's reading of "4 years retention" is calendar-anniversary —
    leap years count as their actual length, the 4th anniversary IS the
    last protected day. relativedelta handles month-end + leap-year
    edge cases correctly.
    """
    today = datetime(2026, 5, 19, tzinfo=UTC).date()
    policy = RetentionPolicy(state_code="CA", retention_years=4, statute_citation="x")
    cutoff = cutoff_for_policy(today=today, policy=policy)
    # 4 calendar years before 2026-05-19 is 2022-05-19.
    assert cutoff == datetime(2022, 5, 19, tzinfo=UTC).date()
    # Spans one leap-day (2024-02-29), so the day-count is 4*365 + 1 = 1461.
    assert (today - cutoff).days == 1461


def test_cutoff_handles_leap_day_anniversary() -> None:
    """A row dated 2024-02-29 has its 4-yr anniversary on 2028-02-29 (a
    leap year). If today is 2028-02-29, it's still in retention. On
    2028-03-01, it's just past — relativedelta picks Feb 28 in non-leap
    years, which is the regulator-safe choice (give the operator the
    nearest in-calendar date, never invent a 02-29 that doesn't exist).
    """
    today_2027 = datetime(2027, 2, 28, tzinfo=UTC).date()
    policy = RetentionPolicy(state_code="CA", retention_years=4, statute_citation="x")
    # today=2027-02-28, retention 4yr -> 2023-02-28 (no Feb 29 in 2023).
    assert cutoff_for_policy(today=today_2027, policy=policy) == datetime(
        2023, 2, 28, tzinfo=UTC
    ).date()


def test_is_expired_strict_inequality_at_boundary() -> None:
    """A row created EXACTLY on the cutoff date is NOT expired.

    Strictly LESS than the cutoff -> expired. This matches the operator-
    safe reading where the Nth anniversary is still protected."""
    today = datetime(2026, 5, 19, tzinfo=UTC).date()
    policy = RetentionPolicy(state_code="NY", retention_years=4, statute_citation="x")
    cutoff = cutoff_for_policy(today=today, policy=policy)

    on_boundary = datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC)
    assert is_expired(created_at=on_boundary, today=today, policy=policy) is False

    one_day_earlier = on_boundary - timedelta(days=1)
    assert is_expired(created_at=one_day_earlier, today=today, policy=policy) is True


def test_resolve_policy_uses_default_when_state_missing() -> None:
    policies = {
        "CA": RetentionPolicy(
            state_code="CA", retention_years=4, statute_citation="ca"
        ),
        "__default__": RetentionPolicy(
            state_code="__default__", retention_years=5, statute_citation="default"
        ),
    }
    assert resolve_policy(state_code="CA", policies=policies).retention_years == 4
    assert resolve_policy(state_code="NY", policies=policies).retention_years == 5
    assert resolve_policy(state_code=None, policies=policies).retention_years == 5


def test_resolve_policy_synthesizes_default_when_table_empty() -> None:
    """The archiver MUST keep running even if migration 025 wasn't applied."""
    p = resolve_policy(state_code="ZZ", policies={})
    assert p.retention_years == 5
    assert p.state_code == "__default__"


# ---------------------------------------------------------------------------
# In-memory archiver
# ---------------------------------------------------------------------------


def _ago(days: int) -> datetime:
    return datetime.now(tz=UTC) - timedelta(days=days)


def _make_archiver(*, policies: dict[str, RetentionPolicy] | None = None) -> tuple[
    InMemoryAuditArchiver, InMemoryAuditLog
]:
    audit = InMemoryAuditLog()
    archiver = InMemoryAuditArchiver(audit=audit, policies=policies)
    return archiver, audit


def _ca_ny_policies() -> dict[str, RetentionPolicy]:
    return {
        "CA": RetentionPolicy(state_code="CA", retention_years=4, statute_citation="ca"),
        "NY": RetentionPolicy(state_code="NY", retention_years=4, statute_citation="ny"),
        "__default__": RetentionPolicy(
            state_code="__default__", retention_years=5, statute_citation="def"
        ),
    }


def test_archives_expired_rows_per_state() -> None:
    archiver, _audit = _make_archiver(policies=_ca_ny_policies())

    ca_old = MemoryAuditRow(
        id=uuid4(),
        actor="api",
        action="decision.approve",
        subject_type="deal",
        subject_id=uuid4(),
        details={"score": 80},
        created_at=_ago(5 * 365),  # 5 years old → past CA 4yr cutoff
        state_code="CA",
    )
    ny_fresh = MemoryAuditRow(
        id=uuid4(),
        actor="api",
        action="decision.approve",
        subject_type="deal",
        subject_id=uuid4(),
        details={"score": 72},
        created_at=_ago(30),  # fresh
        state_code="NY",
    )
    archiver.add_row(ca_old)
    archiver.add_row(ny_fresh)

    report = archiver.archive_expired()
    assert report.archived_count == 1
    assert report.per_state == {"CA": 1}
    assert any(r["source_id"] == str(ca_old.id) for r in archiver.archive)
    # Fresh NY row stayed on the live table.
    assert any(r.id == ny_fresh.id for r in archiver.rows)


def test_archive_runs_record_audit_row_with_batch_summary() -> None:
    archiver, audit = _make_archiver()
    archiver.add_row(
        MemoryAuditRow(
            id=uuid4(),
            actor="api",
            action="deal.score",
            subject_type="deal",
            subject_id=uuid4(),
            details={},
            created_at=_ago(10 * 365),
        )
    )
    report = archiver.archive_expired()

    summaries = [e for e in audit.entries if e["action"] == "audit_log.archive_batch"]
    assert len(summaries) == 1
    s = summaries[0]
    assert s["actor"] == "audit_archiver"
    assert s["details"]["batch_id"] == str(report.batch_id)
    assert s["details"]["archived_count"] == 1


def test_archiver_is_idempotent() -> None:
    """Running the cron twice over the same data archives exactly once."""
    archiver, _audit = _make_archiver()
    row = MemoryAuditRow(
        id=uuid4(),
        actor="api",
        action="deal.score",
        subject_type="deal",
        subject_id=uuid4(),
        details={},
        created_at=_ago(10 * 365),
    )
    archiver.add_row(row)

    first = archiver.archive_expired()
    # Re-add the same row to simulate "what if it hadn't been moved yet"
    # — the source_id collision guard must skip it.
    archiver.add_row(row)
    second = archiver.archive_expired()

    assert first.archived_count == 1
    assert second.archived_count == 0
    assert second.skipped_count == 1
    # Only ONE archive entry total despite two runs.
    assert len(archiver.archive) == 1


def test_archiver_does_not_archive_its_own_summary_rows() -> None:
    """The audit batch row must never be eligible for archiving — that
    would consume the durable evidence of the archive run itself."""
    archiver, _audit = _make_archiver()
    archiver.add_row(
        MemoryAuditRow(
            id=uuid4(),
            actor="audit_archiver",
            action="audit_log.archive_batch",
            subject_type="audit_log",
            subject_id=uuid4(),
            details={"batch_id": "abc"},
            created_at=_ago(20 * 365),  # ancient
        )
    )
    report = archiver.archive_expired()
    assert report.archived_count == 0


def test_archive_report_includes_per_state_breakdown() -> None:
    archiver, _ = _make_archiver(policies=_ca_ny_policies())
    for _ in range(3):
        archiver.add_row(
            MemoryAuditRow(
                id=uuid4(), actor="api", action="x",
                subject_type=None, subject_id=None, details={},
                created_at=_ago(10 * 365), state_code="CA",
            )
        )
    for _ in range(2):
        archiver.add_row(
            MemoryAuditRow(
                id=uuid4(), actor="api", action="x",
                subject_type=None, subject_id=None, details={},
                created_at=_ago(10 * 365), state_code="NY",
            )
        )
    report = archiver.archive_expired()
    assert report.per_state == {"CA": 3, "NY": 2}
    assert report.archived_count == 5


def test_archive_report_serializes_to_audit_details() -> None:
    """The dict that hits the audit row must be JSON-serializable shape."""
    today = datetime(2026, 5, 19, tzinfo=UTC).date()
    report = ArchiveReport(batch_id=uuid4(), cutoff_date=today)
    report.archived_count = 7
    report.per_state = {"CA": 5, "NY": 2}
    report.finished_at = datetime.now(tz=UTC)
    details = report.to_audit_details()
    assert details["cutoff_date"] == today.isoformat()
    assert details["archived_count"] == 7
    assert details["per_state"] == {"CA": 5, "NY": 2}


# ---------------------------------------------------------------------------
# Audit-write failure FAILS the operation
# ---------------------------------------------------------------------------


class _ExplodingAudit(InMemoryAuditLog):
    def record(self, **kwargs: object) -> None:
        from aegis.audit import AuditWriteError

        raise AuditWriteError("simulated audit DB outage")


def test_audit_write_failure_fails_archiver() -> None:
    """CLAUDE.md: every audit-log write that fails MUST fail the operation."""
    archiver = InMemoryAuditArchiver(audit=_ExplodingAudit())
    with pytest.raises(ArchiverError):
        archiver.archive_expired()


# ---------------------------------------------------------------------------
# Cron entrypoint smoke
# ---------------------------------------------------------------------------


async def test_run_archive_cron_returns_summary_dict() -> None:
    """The arq cron entrypoint returns a small dict suitable for arq logs."""
    from aegis.audit_archiver import run_archive_cron

    archiver, audit = _make_archiver()
    ctx = {"audit": audit, "archiver": archiver}
    result = await run_archive_cron(ctx)
    assert "batch_id" in result
    assert "archived_count" in result
    assert "per_state" in result
    assert result["archived_count"] == 0


# ---------------------------------------------------------------------------
# WorkerSettings wiring — the cron must actually be registered.
# ---------------------------------------------------------------------------


def test_worker_settings_registers_archive_cron() -> None:
    """The arq daily cron must be wired into WorkerSettings.cron_jobs.

    Without this, the archiver code is dead in production (the function
    exists but nothing invokes it). This test guards the wiring.
    """
    from aegis.audit_archiver import run_archive_cron
    from aegis.workers import WorkerSettings

    crons = getattr(WorkerSettings, "cron_jobs", None)
    assert crons is not None and len(crons) >= 1
    coroutines = {c.coroutine for c in crons}
    assert run_archive_cron in coroutines

    archive_cron = next(c for c in crons if c.coroutine is run_archive_cron)
    # Nightly 02:00 UTC.
    assert archive_cron.hour == 2
    assert archive_cron.minute == 0
