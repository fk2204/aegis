"""Compliance obligations module tests (mp Phase 7 §17).

Covers the derived-state logic + the summarize aggregator. Reading from a
live Supabase is integration-tested separately; the InMemory repo gives
full coverage of the pure annotation pass.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from aegis.compliance.obligations import (
    InMemoryObligationsRepository,
    ObligationRow,
    _annotate_rows,
    _apply_derived_state,
    summarize,
)


def _row(
    *,
    state_code: str = "CA",
    status: str = "not_started",
    next_due_date: date | None = None,
    deadline: date | None = None,
) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "obligation_type": "registration",
        "state_code": state_code,
        "authority": f"{state_code} DFI",
        "description": "Annual broker registration.",
        "deadline": deadline.isoformat() if deadline else None,
        "recurrence": "annual",
        "status": status,
        "next_due_date": next_due_date.isoformat() if next_due_date else None,
        "evidence_file_path": None,
        "last_reviewed": None,
        "notes": None,
    }


def test_overdue_row_when_next_due_in_past_and_not_submitted() -> None:
    rows = _annotate_rows(
        [_row(next_due_date=date.today() - timedelta(days=10))],
        today=date.today(),
    )
    assert len(rows) == 1
    assert rows[0].derived_state == "overdue"
    assert rows[0].days_until_due == -10


def test_due_soon_row_within_60_day_horizon() -> None:
    rows = _annotate_rows(
        [_row(next_due_date=date.today() + timedelta(days=15))],
        today=date.today(),
    )
    assert rows[0].derived_state == "due_soon"


def test_on_track_row_far_future() -> None:
    rows = _annotate_rows(
        [_row(next_due_date=date.today() + timedelta(days=180))],
        today=date.today(),
    )
    assert rows[0].derived_state == "on_track"


def test_no_deadline_row_when_both_dates_null() -> None:
    rows = _annotate_rows([_row()], today=date.today())
    assert rows[0].derived_state == "no_deadline"
    assert rows[0].days_until_due is None


def test_submitted_status_suppresses_overdue() -> None:
    """Once the operator records the renewal as submitted, the obligation
    is met until the next recurrence rolls over the next_due_date. Even
    if the original deadline is in the past, the UI shouldn't keep
    screaming OVERDUE."""
    rows = _annotate_rows(
        [_row(status="submitted", deadline=date.today() - timedelta(days=10))],
        today=date.today(),
    )
    assert rows[0].derived_state == "on_track"


def test_active_status_also_treated_as_on_track() -> None:
    rows = _annotate_rows(
        [_row(status="active", next_due_date=date.today() + timedelta(days=10))],
        today=date.today(),
    )
    assert rows[0].derived_state == "on_track"


def test_sort_order_urgency_first() -> None:
    """The dashboard table reads top-down: overdue rows come first so the
    operator's eye lands on the worst items immediately."""
    rows_in = [
        _row(state_code="CA", next_due_date=date.today() + timedelta(days=180)),  # on_track
        _row(state_code="NY", next_due_date=date.today() - timedelta(days=1)),    # overdue
        _row(state_code="TX", next_due_date=date.today() + timedelta(days=20)),   # due_soon
    ]
    rows = _annotate_rows(rows_in, today=date.today())
    assert [r.state_code for r in rows] == ["NY", "TX", "CA"]


def test_summarize_counts() -> None:
    rows = _annotate_rows(
        [
            _row(state_code="CA", next_due_date=date.today() - timedelta(days=5)),
            _row(state_code="NY", next_due_date=date.today() + timedelta(days=10)),
            _row(state_code="TX", status="submitted"),
            _row(state_code="VA", next_due_date=date.today() + timedelta(days=120)),
        ],
        today=date.today(),
    )
    summary = summarize(rows)
    assert summary.total == 4
    assert summary.overdue == 1
    assert summary.due_soon == 1
    assert summary.on_track >= 1
    assert summary.submitted_or_active == 1


def test_in_memory_repository_returns_annotated_rows() -> None:
    repo = InMemoryObligationsRepository(
        rows=[
            _row(next_due_date=date.today() + timedelta(days=5)),
        ]
    )
    rows = repo.list_obligations()
    assert rows[0].derived_state == "due_soon"


def test_earliest_of_next_due_or_deadline_wins() -> None:
    """When both deadline and next_due_date are set, the earlier date
    governs urgency — operator should never miss the harder cutoff."""
    row = ObligationRow(
        id=str(uuid4()),
        obligation_type="registration",
        state_code="TX",
        authority="TX OCCC",
        description="...",
        deadline=date.today() + timedelta(days=10),
        next_due_date=date.today() + timedelta(days=90),
        status="not_started",
    )
    annotated = _apply_derived_state(row, today=date.today())
    assert annotated.derived_state == "due_soon"
    assert annotated.days_until_due == 10
