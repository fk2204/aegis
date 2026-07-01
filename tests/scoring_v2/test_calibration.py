"""Tests for the calibration engine (2026-07-01 FIX 3).

``compute_and_store`` reads ``funder_replies`` + ``analyses`` +
``decisions`` and returns a ``CalibrationResult`` snapshot. When the
outcome count is below ``MIN_OUTCOMES`` it returns a snapshot with
``outcome_count=<the actual count>`` and does NOT write a row to
``calibration_snapshots``. Above the floor it computes the accuracy
metrics and persists the snapshot.

These tests exercise the low-outcome / empty / threshold paths using
a hand-rolled fake for the Supabase client — the calibration read
path is a small number of ``.table().select().execute()`` chains, so
the fake stays tiny.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from aegis.scoring_v2.calibration import (
    MIN_OUTCOMES,
    CalibrationResult,
    compute_and_store,
)


class _FakeQuery:
    """Chainable stand-in for a Supabase query builder that returns a
    fixed list of dicts on ``.execute()``. Every call to a filter or
    selector returns ``self`` so the chain matches Supabase-py."""

    def __init__(self, data: list[dict[str, Any]]) -> None:
        self._data = data

    def select(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return self

    def in_(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return self

    def eq(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return self

    def order(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return self

    def limit(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return self

    def insert(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return self

    def execute(self) -> Any:
        class _Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        return _Result(self._data)


class _FakeSupabase:
    """Dispatches by table name to a fake query. Missing tables return
    an empty result — the calibration path tolerates that."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self._tables.get(name, []))


def test_returns_zero_snapshot_when_no_outcomes() -> None:
    """Empty ``funder_replies`` — compute_and_store returns a snapshot
    with outcome_count=0 (does NOT write a row)."""
    sb = _FakeSupabase({"funder_replies": []})
    result = compute_and_store(sb)
    assert isinstance(result, CalibrationResult)
    assert result.outcome_count == 0
    assert result.fraud_true_positive_rate == 0.0


def test_min_outcomes_in_valid_range() -> None:
    """The floor is user-visible in the dashboard; keep it in a
    defensible range so a config typo doesn't silently disable the
    engine."""
    assert 10 <= MIN_OUTCOMES <= 50


def test_returns_snapshot_below_threshold_with_count() -> None:
    """Below-floor outcomes still return a snapshot — the cron logs
    the count so the operator sees progress toward the threshold."""
    short = [
        {
            "id": f"reply_{i}",
            "merchant_id": f"mid_{i:04d}",
            "outcome": "approved",
            "deal_id": None,
            "submission_id": f"sub_{i}",
            "created_at": "2026-06-01T00:00:00Z",
        }
        for i in range(MIN_OUTCOMES - 1)
    ]
    sb = _FakeSupabase({"funder_replies": short})
    result = compute_and_store(sb)
    assert isinstance(result, CalibrationResult)
    assert result.outcome_count == MIN_OUTCOMES - 1


def test_does_not_raise_on_empty_db() -> None:
    """Every Supabase table empty — the calibration engine must not
    raise. Silent zero-metrics is preferable to a crashed cron."""
    sb = _FakeSupabase({})
    try:
        result = compute_and_store(sb)
    except Exception as exc:
        raise AssertionError(f"Should not raise on empty db: {exc}") from exc
    assert isinstance(result, CalibrationResult)


def test_accepts_magicmock_style_stub() -> None:
    """MagicMock is a common test-side substitute; verify the calling
    convention doesn't blow up on the auto-mocked chain."""
    mock_sb = MagicMock()
    # Force .data to an empty list so the outcome_count read is
    # deterministic — otherwise MagicMock auto-generates a Mock() and
    # ``[r for r in ... if isinstance(r, dict)]`` filters to [].
    mock_sb.table.return_value.select.return_value.execute.return_value.data = []
    result = compute_and_store(mock_sb)
    assert isinstance(result, CalibrationResult)
