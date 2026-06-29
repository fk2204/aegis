"""Tests for the compliance-obligation tracker (migration 069).

Covers three surfaces stitched together by Part A + B + C of the spec:

  * ``ComplianceObligationRepository.list_upcoming`` returns the right
    slice + sort + urgency bucketing.
  * ``ComplianceObligationRepository.mark_status`` mutates the row AND
    writes one ``compliance.obligation_status_changed`` audit row.
  * ``run_compliance_obligation_reminder_pass`` fires
    ``compliance.deadline_approaching`` audit rows at the 60 / 30 /
    14-day thresholds AND not at other days, AND is dedup-keyed so a
    weekly cadence doesn't double-fire.
  * ``run_compliance_obligation_reminder_cron`` is wired into
    ``WorkerSettings.cron_jobs`` at Mon 07:00 UTC.
  * Today-dashboard ``build_compliance_attention_section`` returns the
    right urgency buckets (red <=14, amber <=30, yellow <=60) so the
    template renders the right color classes.
  * The Today dashboard route renders the "Compliance deadlines"
    section without 500 and surfaces the color buckets in the markup.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import reset_dependency_caches
from aegis.audit import InMemoryAuditLog
from aegis.compliance import obligations as obligations_mod
from aegis.compliance.obligations import (
    REMINDER_THRESHOLD_DAYS,
    TODAY_CARD_HORIZON_DAYS,
    InMemoryComplianceObligationRepository,
    ObligationNotFoundError,
    build_compliance_attention_section,
    run_compliance_obligation_reminder_cron,
    run_compliance_obligation_reminder_pass,
)

# ---------------------------------------------------------------------------
# Row factory
# ---------------------------------------------------------------------------


def _seed_row(
    *,
    obligation_id: UUID | None = None,
    state_code: str = "TX",
    authority: str = "TX OCCC",
    description: str = "Annual broker registration.",
    status: str = "not_started",
    next_due_date: date | None = None,
) -> dict[str, Any]:
    return {
        "id": str(obligation_id or uuid4()),
        "obligation_type": "registration",
        "state_code": state_code,
        "authority": authority,
        "description": description,
        "deadline": None,
        "recurrence": "annual",
        "status": status,
        "next_due_date": (next_due_date.isoformat() if next_due_date is not None else None),
        "evidence_file_path": None,
        "last_reviewed": None,
        "notes": None,
    }


# ===========================================================================
# list_upcoming — right slice + sort
# ===========================================================================


def test_list_upcoming_returns_rows_within_window() -> None:
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(next_due_date=today + timedelta(days=10)),
            _seed_row(next_due_date=today + timedelta(days=120)),  # outside
            _seed_row(next_due_date=None),  # no deadline
        ]
    )
    upcoming = repo.list_upcoming(60, today=today)
    assert len(upcoming) == 1
    assert upcoming[0].days_remaining == 10


def test_list_upcoming_includes_overdue() -> None:
    """Past-due rows must surface on the Today card even after the
    deadline passes — the operator needs the visibility."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[_seed_row(next_due_date=today - timedelta(days=3))]
    )
    upcoming = repo.list_upcoming(60, today=today)
    assert len(upcoming) == 1
    assert upcoming[0].days_remaining == -3


def test_list_upcoming_sorts_by_next_due_date_ascending() -> None:
    today = date.today()
    later = today + timedelta(days=45)
    sooner = today + timedelta(days=10)
    middle = today + timedelta(days=20)
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(state_code="CA", next_due_date=later),
            _seed_row(state_code="TX", next_due_date=sooner),
            _seed_row(state_code="VA", next_due_date=middle),
        ]
    )
    upcoming = repo.list_upcoming(60, today=today)
    assert [u.state_code for u in upcoming] == ["TX", "VA", "CA"]


def test_list_upcoming_urgency_buckets() -> None:
    """Red <=14 days, amber <=30, yellow <=60 — matches the Today card
    color classes."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(state_code="A1", next_due_date=today + timedelta(days=5)),
            _seed_row(state_code="A2", next_due_date=today + timedelta(days=14)),
            _seed_row(state_code="B1", next_due_date=today + timedelta(days=15)),
            _seed_row(state_code="B2", next_due_date=today + timedelta(days=30)),
            _seed_row(state_code="C1", next_due_date=today + timedelta(days=31)),
            _seed_row(state_code="C2", next_due_date=today + timedelta(days=60)),
        ]
    )
    upcoming = repo.list_upcoming(60, today=today)
    by_state = {u.state_code: u.urgency for u in upcoming}
    assert by_state == {
        "A1": "red",
        "A2": "red",
        "B1": "amber",
        "B2": "amber",
        "C1": "yellow",
        "C2": "yellow",
    }


def test_list_upcoming_skips_null_next_due_date() -> None:
    """An obligation without a next_due_date never fires a reminder and
    never lands on the Today card — per the spec's NULL = no reminder
    rule."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(state_code="MO", next_due_date=None),
        ]
    )
    assert repo.list_upcoming(60, today=today) == []


# ===========================================================================
# mark_status — writes audit row, raises on missing id
# ===========================================================================


def test_mark_status_mutates_row_and_writes_audit() -> None:
    today = date.today()
    obligation_id = uuid4()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(
                obligation_id=obligation_id,
                state_code="TX",
                authority="TX OCCC",
                next_due_date=today + timedelta(days=10),
            )
        ]
    )
    audit = InMemoryAuditLog()

    repo.mark_status(obligation_id, "submitted", audit=audit, actor="filip")

    assert repo.rows[0]["status"] == "submitted"
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["actor"] == "filip"
    assert entry["action"] == "compliance.obligation_status_changed"
    assert entry["subject_type"] == "compliance_obligation"
    assert entry["subject_id"] == str(obligation_id)
    details = entry["details"]
    assert details["from_status"] == "not_started"
    assert details["to_status"] == "submitted"
    assert details["state_code"] == "TX"
    assert details["authority"] == "TX OCCC"


def test_mark_status_raises_on_unknown_id() -> None:
    repo = InMemoryComplianceObligationRepository(rows=[])
    audit = InMemoryAuditLog()
    with pytest.raises(ObligationNotFoundError):
        repo.mark_status(uuid4(), "submitted", audit=audit)


# ===========================================================================
# Cron — fires at 60 / 30 / 14, dedupes, skips submitted/active
# ===========================================================================


def test_cron_fires_audit_at_60_day_threshold() -> None:
    today = date.today()
    obligation_id = uuid4()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(
                obligation_id=obligation_id,
                state_code="CT",
                authority="CT DOB",
                description="CT DOB annual.",
                next_due_date=today + timedelta(days=58),
            )
        ]
    )
    audit = InMemoryAuditLog()

    summary = run_compliance_obligation_reminder_pass(
        audit=audit,
        obligations=repo,
        today=today,
    )

    assert summary["fired"] == 1
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["action"] == "compliance.deadline_approaching"
    details = entry["details"]
    assert details["threshold_days"] == 60
    assert details["days_remaining"] == 58
    assert details["authority"] == "CT DOB"
    assert details["state_code"] == "CT"
    assert details["obligation_description"] == "CT DOB annual."
    assert details["deadline"] == (today + timedelta(days=58)).isoformat()


def test_cron_fires_audit_at_30_day_threshold() -> None:
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[_seed_row(next_due_date=today + timedelta(days=29))]
    )
    audit = InMemoryAuditLog()

    summary = run_compliance_obligation_reminder_pass(audit=audit, obligations=repo, today=today)

    # The cron walks 60 → 30 → 14 descending. On a 29-day obligation
    # seen for the first time, BOTH the 60-day and 30-day thresholds
    # fire on this run (29 <= 30 and 29 <= 60, neither prior reminder
    # exists). 14 does NOT fire (29 > 14). Operator-safe: a missed
    # cron run can't drop the 60-day reminder.
    assert summary["fired"] == 2
    actions = [
        e["details"]["threshold_days"]
        for e in audit.entries
        if e["action"] == "compliance.deadline_approaching"
    ]
    assert set(actions) == {60, 30}
    assert 14 not in actions


def test_cron_fires_audit_at_14_day_threshold() -> None:
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[_seed_row(next_due_date=today + timedelta(days=10))]
    )
    audit = InMemoryAuditLog()

    summary = run_compliance_obligation_reminder_pass(audit=audit, obligations=repo, today=today)

    assert summary["fired"] >= 1
    thresholds_fired = {
        e["details"]["threshold_days"]
        for e in audit.entries
        if e["action"] == "compliance.deadline_approaching"
    }
    assert 14 in thresholds_fired


def test_cron_does_not_fire_for_far_future_or_no_deadline() -> None:
    """The cron must NOT fire on days outside the thresholds. A 61-day
    obligation produces zero audit rows; a NULL-next_due_date row is
    invisible."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(state_code="A", next_due_date=today + timedelta(days=61)),
            _seed_row(state_code="B", next_due_date=None),
        ]
    )
    audit = InMemoryAuditLog()

    summary = run_compliance_obligation_reminder_pass(audit=audit, obligations=repo, today=today)

    assert summary["fired"] == 0
    assert audit.entries == []


def test_cron_dedupes_subsequent_runs() -> None:
    """A second run on the same day must NOT re-fire reminders for the
    same (obligation, threshold, next_due_date) tuple — the weekly
    cadence should not double-fire the 60-day reminder."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[_seed_row(next_due_date=today + timedelta(days=50))]
    )
    audit = InMemoryAuditLog()

    first = run_compliance_obligation_reminder_pass(audit=audit, obligations=repo, today=today)
    second = run_compliance_obligation_reminder_pass(audit=audit, obligations=repo, today=today)

    # First run fires the 60-threshold; second run dedupes.
    assert first["fired"] == 1
    assert second["fired"] == 0
    assert second["skipped_already_fired"] >= 1


def test_cron_skips_submitted_and_active_status() -> None:
    """Status met → no reminder. The obligation is satisfied until the
    next recurrence rolls the next_due_date forward."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(
                state_code="VA",
                status="submitted",
                next_due_date=today + timedelta(days=5),
            ),
            _seed_row(
                state_code="UT",
                status="active",
                next_due_date=today + timedelta(days=10),
            ),
        ]
    )
    audit = InMemoryAuditLog()

    summary = run_compliance_obligation_reminder_pass(audit=audit, obligations=repo, today=today)

    assert summary["fired"] == 0
    assert summary["skipped_status_met"] == 2


async def test_cron_async_entrypoint_uses_ctx_overrides() -> None:
    """The arq entrypoint reads ``ctx['audit']`` + ``ctx['obligations']``
    so tests inject fakes. Mirrors the renewal-reminder cron pattern."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[_seed_row(next_due_date=today + timedelta(days=12))]
    )
    audit = InMemoryAuditLog()

    summary = await run_compliance_obligation_reminder_cron({"audit": audit, "obligations": repo})

    # 12 days out → fires 60, 30, AND 14 thresholds in one run.
    assert summary["fired"] == 3
    thresholds = {
        e["details"]["threshold_days"]
        for e in audit.entries
        if e["action"] == "compliance.deadline_approaching"
    }
    assert thresholds == {60, 30, 14}


# ===========================================================================
# WorkerSettings registration
# ===========================================================================


def test_cron_registered_at_mon_0700_utc() -> None:
    """The cron must be wired into WorkerSettings.cron_jobs or arq won't
    pick it up. Schedule: weekday=mon, 07:00 UTC."""
    from aegis.workers import WorkerSettings

    target = "run_compliance_obligation_reminder_cron"
    matches = [
        job
        for job in WorkerSettings.cron_jobs
        if getattr(job, "name", "") in (target, f"cron:{target}")
    ]
    assert len(matches) == 1, (
        f"expected one cron for {target!r}; "
        f"got {[getattr(j, 'name', '?') for j in WorkerSettings.cron_jobs]}"
    )
    cron_job = matches[0]
    assert cron_job.weekday == "mon"
    assert cron_job.hour == 7
    assert cron_job.minute == 0


def test_reminder_thresholds_are_60_30_14() -> None:
    """The operator's spec: 60 / 30 / 14 days before next_due_date.
    Catches accidental tuple edits."""
    assert REMINDER_THRESHOLD_DAYS == (60, 30, 14)


def test_today_card_horizon_is_180_days() -> None:
    """Today-dashboard attention window is 180 days.

    Widened from 90 → 180 on 2026-06-28 so the TX OCCC 2026-12-31
    filing (~186 days lead time) surfaces on the Today card with
    operator runway before the 60-day reminder cron fires. Reminder
    cron thresholds remain 60/30/14 — that contract is locked
    separately in ``test_reminder_thresholds_are_60_30_14``.
    """
    assert TODAY_CARD_HORIZON_DAYS == 180


def test_build_compliance_attention_section_surfaces_180_day_obligations() -> None:
    """A TX OCCC-style filing ~150 days out must surface on the Today
    card. Pre-widen this row sat outside the 90-day window and the
    operator lost lead time."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(
                state_code="TX",
                authority="TX OCCC",
                next_due_date=today + timedelta(days=150),
            ),
        ]
    )

    count, _source_ids, cards = build_compliance_attention_section(repo)

    assert count == 1
    assert len(cards) == 1
    assert cards[0].state_code == "TX"
    # UTC/local date boundary can shift this by one day; the contract
    # is "surfaces inside the widened 180-day window", not exact day
    # arithmetic.
    assert 148 <= cards[0].days_remaining <= 150


# ===========================================================================
# Today dashboard card builder
# ===========================================================================


def test_build_compliance_attention_section_pairs_count_with_source_ids() -> None:
    """Per CLAUDE.md auditability: every aggregate carries the source
    ID list that produced it. The Today card aggregate count must pair
    with the contributing obligation_id list."""
    today = date.today()
    ids = [uuid4(), uuid4(), uuid4()]
    repo = InMemoryComplianceObligationRepository(
        rows=[
            _seed_row(
                obligation_id=ids[0],
                state_code="TX",
                next_due_date=today + timedelta(days=10),
            ),
            _seed_row(
                obligation_id=ids[1],
                state_code="VA",
                next_due_date=today + timedelta(days=30),
            ),
            _seed_row(
                obligation_id=ids[2],
                state_code="CA",
                next_due_date=today + timedelta(days=80),
            ),
        ]
    )

    count, source_ids, cards = build_compliance_attention_section(repo)

    assert count == 3
    assert set(source_ids) == {str(i) for i in ids}
    # Cards carry the urgency bucket so the template can color them.
    by_state = {c.state_code: c.urgency for c in cards}
    assert by_state == {"TX": "red", "VA": "amber", "CA": "yellow"}


def test_build_compliance_attention_section_caps_card_list_but_not_count() -> None:
    """When the operator has more than the cap, ``count`` reflects the
    full backlog and ``source_obligation_ids`` carries every id, but
    ``cards`` is truncated."""
    today = date.today()
    repo = InMemoryComplianceObligationRepository(
        rows=[_seed_row(next_due_date=today + timedelta(days=5 + i)) for i in range(20)]
    )

    count, source_ids, cards = build_compliance_attention_section(repo, cap=5)

    assert count == 20
    assert len(source_ids) == 20
    assert len(cards) == 5


# ===========================================================================
# Today dashboard route render
# ===========================================================================


@pytest.fixture
def today_dashboard_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient with the compliance repo factory stubbed.

    Three rows give the template a red + amber + yellow card so the
    color-bucket data attributes assert end-to-end. The other Today
    fixtures (merchants / docs / funder_note_subs) all default to empty
    via the in-memory backend.
    """
    today = date.today()
    seeded = [
        _seed_row(
            state_code="TX",
            authority="TX OCCC",
            description="TX OCCC registration filing.",
            next_due_date=today + timedelta(days=10),
        ),
        _seed_row(
            state_code="VA",
            authority="VA SCC",
            description="VA SCC annual.",
            next_due_date=today + timedelta(days=25),
        ),
        _seed_row(
            state_code="CT",
            authority="CT DOB",
            description="CT DOB annual.",
            next_due_date=today + timedelta(days=50),
        ),
    ]

    def _factory() -> obligations_mod.ComplianceObligationRepository:
        return InMemoryComplianceObligationRepository(rows=seeded)

    monkeypatch.setattr(
        obligations_mod,
        "get_compliance_obligation_repository",
        _factory,
    )
    # The dashboard router imported the name at module level; patch
    # both modules so the route uses the seeded factory.
    monkeypatch.setattr(
        "aegis.web.routers.dashboard.get_compliance_obligation_repository",
        _factory,
    )

    reset_dependency_caches()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_dependency_caches()


def test_today_dashboard_renders_compliance_card_with_color_buckets(
    today_dashboard_client: TestClient,
) -> None:
    resp = today_dashboard_client.get("/ui/")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Section header surfaces with the seeded count.
    assert "Compliance deadlines" in body
    # All three authorities render.
    assert "TX OCCC" in body
    assert "VA SCC" in body
    assert "CT DOB" in body
    # Color buckets surface on the row data-urgency attribute so the
    # template can target each color independently.
    assert 'data-urgency="red"' in body
    assert 'data-urgency="amber"' in body
    assert 'data-urgency="yellow"' in body
    # Section test-id surfaces.
    assert "today-attn-compliance" in body


def test_today_dashboard_empty_compliance_renders_empty_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No upcoming obligations → "No compliance deadlines…" empty
    copy renders without breaking the page."""

    def _factory() -> obligations_mod.ComplianceObligationRepository:
        return InMemoryComplianceObligationRepository(rows=[])

    monkeypatch.setattr(
        obligations_mod,
        "get_compliance_obligation_repository",
        _factory,
    )
    monkeypatch.setattr(
        "aegis.web.routers.dashboard.get_compliance_obligation_repository",
        _factory,
    )

    reset_dependency_caches()
    app = create_app()
    try:
        with TestClient(app) as client:
            resp = client.get("/ui/")
            assert resp.status_code == 200, resp.text
            assert "No compliance deadlines" in resp.text
            assert "today-attn-compliance" in resp.text
    finally:
        reset_dependency_caches()
