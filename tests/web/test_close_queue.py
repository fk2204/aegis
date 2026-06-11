"""Pin the stale-row predicates in `_classify_close_pipeline_state`.

Plan 3.2 lock-in. The route at ``src/aegis/web/routers/close_queue.py``
already ships the staleness detection (6h pull timeout, 1h parse
timeout); this file pins the behavior so a future refactor cannot
silently break the contract that surfaces stuck merchants on
``/ui/close-queue``.

The classifier is pure: ``(docs, audit_rows, now) -> state-dict``. All
tests construct fixed `now` + audit rows / docs at controlled offsets.
No clock mocking needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aegis.storage import DocumentRow
from aegis.web.routers.close_queue import (
    _CLOSE_QUEUE_STALE_PARSE_HOURS,
    _CLOSE_QUEUE_STALE_PULL_HOURS,
    _classify_close_pipeline_state,
)

_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _enqueued_row(hours_ago: float, *, action: str = "close.orchestration.enqueued") -> dict[str, object]:
    """An audit-log row simulating a Close pull enqueued ``hours_ago`` hours back."""
    return {
        "action": action,
        "created_at": (_NOW - timedelta(hours=hours_ago)).isoformat(),
        "details": {},
    }


def _doc(parse_status: str, *, uploaded_hours_ago: float) -> DocumentRow:
    """Minimal DocumentRow at a controlled upload time."""
    return DocumentRow(
        id=uuid4(),
        file_hash=f"sha256-{uuid4().hex}",
        byte_size=1024,
        original_filename="stmt.pdf",
        parse_status=parse_status,
        uploaded_at=_NOW - timedelta(hours=uploaded_hours_ago),
    )


# --------------------------------------------------------------------
# Stale-PULL predicate (close.orchestration.enqueued > 6h, no docs)
# --------------------------------------------------------------------


def test_stale_pull_fires_after_threshold() -> None:
    """Pull enqueued past the 6h floor with no docs → state=stuck."""
    enqueued_h = _CLOSE_QUEUE_STALE_PULL_HOURS + 1.0  # 7h
    state = _classify_close_pipeline_state(
        docs=[], audit_rows=[_enqueued_row(enqueued_h)], now=_NOW
    )
    assert state["state"] == "stuck"
    assert state["action"] == "retry"
    assert "no pull" in state["label"].lower()


def test_fresh_pull_is_awaiting_not_stuck() -> None:
    """Pull enqueued well before the floor → awaiting_pull."""
    enqueued_h = _CLOSE_QUEUE_STALE_PULL_HOURS - 1.0  # 5h
    state = _classify_close_pipeline_state(
        docs=[], audit_rows=[_enqueued_row(enqueued_h)], now=_NOW
    )
    assert state["state"] == "awaiting_pull"
    assert state["action"] is None


def test_stale_pull_boundary_just_above() -> None:
    """``elapsed > _CLOSE_QUEUE_STALE_PULL_HOURS`` — strict ``>``.

    At exactly the threshold the predicate does NOT fire (the route uses
    `>` not `>=`). One nanosecond past does.
    """
    just_over_h = _CLOSE_QUEUE_STALE_PULL_HOURS + 0.001  # 6.001h
    state_over = _classify_close_pipeline_state(
        docs=[], audit_rows=[_enqueued_row(just_over_h)], now=_NOW
    )
    assert state_over["state"] == "stuck"

    just_under_h = _CLOSE_QUEUE_STALE_PULL_HOURS  # exactly 6h
    state_at = _classify_close_pipeline_state(
        docs=[], audit_rows=[_enqueued_row(just_under_h)], now=_NOW
    )
    assert state_at["state"] == "awaiting_pull"


def test_manual_rescan_same_stale_window() -> None:
    """A manual-rescan audit row is handled identically to enqueued."""
    state = _classify_close_pipeline_state(
        docs=[],
        audit_rows=[
            _enqueued_row(
                _CLOSE_QUEUE_STALE_PULL_HOURS + 0.5,
                action="close.orchestration.manual_rescan",
            )
        ],
        now=_NOW,
    )
    assert state["state"] == "stuck"


# --------------------------------------------------------------------
# Stale-PARSE predicate (any pending doc uploaded > 1h ago)
# --------------------------------------------------------------------


def test_stale_parse_fires_after_threshold() -> None:
    """A pending doc older than 1h → state=stuck (parse)."""
    pending = _doc("pending", uploaded_hours_ago=_CLOSE_QUEUE_STALE_PARSE_HOURS + 1.0)
    state = _classify_close_pipeline_state(
        docs=[pending],
        audit_rows=[_enqueued_row(0.1)],  # pull recently completed
        now=_NOW,
    )
    assert state["state"] == "stuck"
    assert state["action"] == "retry"
    assert "parse" in state["label"].lower()


def test_fresh_parse_is_parsing_not_stuck() -> None:
    """Pending doc inside the 1h window → state=parsing (informational)."""
    pending = _doc("pending", uploaded_hours_ago=_CLOSE_QUEUE_STALE_PARSE_HOURS - 0.5)
    state = _classify_close_pipeline_state(
        docs=[pending], audit_rows=[_enqueued_row(0.1)], now=_NOW
    )
    assert state["state"] == "parsing"
    assert state["action"] is None


def test_stale_parse_boundary_just_above() -> None:
    """``elapsed > _CLOSE_QUEUE_STALE_PARSE_HOURS`` — strict ``>``."""
    just_over = _doc(
        "pending", uploaded_hours_ago=_CLOSE_QUEUE_STALE_PARSE_HOURS + 0.001
    )
    state_over = _classify_close_pipeline_state(
        docs=[just_over], audit_rows=[_enqueued_row(0.1)], now=_NOW
    )
    assert state_over["state"] == "stuck"

    at_floor = _doc(
        "pending", uploaded_hours_ago=_CLOSE_QUEUE_STALE_PARSE_HOURS
    )
    state_at = _classify_close_pipeline_state(
        docs=[at_floor], audit_rows=[_enqueued_row(0.1)], now=_NOW
    )
    assert state_at["state"] == "parsing"


def test_stale_parse_uses_oldest_pending_doc() -> None:
    """When multiple pending docs exist, the OLDEST drives the elapsed
    calculation — a fresh doc cannot mask a stale sibling."""
    docs = [
        _doc("pending", uploaded_hours_ago=0.1),  # fresh
        _doc("pending", uploaded_hours_ago=_CLOSE_QUEUE_STALE_PARSE_HOURS + 1.0),
        _doc("pending", uploaded_hours_ago=0.5),  # fresh
    ]
    state = _classify_close_pipeline_state(
        docs=docs, audit_rows=[_enqueued_row(0.1)], now=_NOW
    )
    assert state["state"] == "stuck"
    assert "3 document(s) pending" in state["detail"]


# --------------------------------------------------------------------
# Non-stale paths (regression guards on the happy classifications)
# --------------------------------------------------------------------


def test_clean_docs_only_score() -> None:
    """All docs proceed / review with no pending → state=scored."""
    docs = [
        _doc("proceed", uploaded_hours_ago=0.5),
        _doc("review", uploaded_hours_ago=0.5),
    ]
    state = _classify_close_pipeline_state(
        docs=docs, audit_rows=[_enqueued_row(0.1)], now=_NOW
    )
    assert state["state"] == "scored"
    assert state["action"] is None
    assert state["severity"] == "good"


def test_no_audit_no_docs_is_stuck() -> None:
    """Merchant created but never enqueued → state=stuck (no audit)."""
    state = _classify_close_pipeline_state(docs=[], audit_rows=[], now=_NOW)
    assert state["state"] == "stuck"
    assert state["detail"] == "No Close orchestration audit on file"


def test_list_failed_audit_routes_to_failed_pull() -> None:
    """A close.orchestration.list_failed audit row → state=failed_pull."""
    audit = [
        {
            "action": "close.orchestration.list_failed",
            "created_at": (_NOW - timedelta(minutes=10)).isoformat(),
            "details": {"message": "401 unauthorized from Close"},
        }
    ]
    state = _classify_close_pipeline_state(docs=[], audit_rows=audit, now=_NOW)
    assert state["state"] == "failed_pull"
    assert state["severity"] == "bad"
    assert "401" in state["detail"]


# --------------------------------------------------------------------
# Threshold constants — pin the values themselves
# --------------------------------------------------------------------


def test_threshold_constants_match_spec() -> None:
    """6h pull + 1h parse per docs/CLOSE_AUTOMATION_SPEC.md Step 4.

    A future tuning of these constants is fine, but it should be
    deliberate — flag any unintentional drift here.
    """
    assert _CLOSE_QUEUE_STALE_PULL_HOURS == pytest.approx(6.0)
    assert _CLOSE_QUEUE_STALE_PARSE_HOURS == pytest.approx(1.0)
