"""Unit tests for ``scripts/tampering_shadow_review.py``.

Covers the pure-function core (parse_audit_row / parse_rows /
summarize / write_csv / summary_lines / fetch_rows server-side
narrowing) and the CLI dispatch via DI. No Supabase calls.
"""

from __future__ import annotations

import io
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from scripts import tampering_shadow_review as review  # noqa: E402

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _shadow_row(
    *,
    document_id: str = "doc-aaa",
    branch: str = "editor_plus_drift",
    metadata_score: int = 38,
    math_score: int = 22,
    failures: tuple[str, ...] = ("reconciliation_failed_period",),
    rationale: str = "iText 2.1.7 editor + balance drift across 4 months",
    created_at: str = "2026-06-10T18:00:00Z",
) -> dict[str, Any]:
    """One canonical shadow-mode audit_log row, shape matching production."""
    return {
        "action": review.SHADOW_ACTION,
        "subject_type": "document",
        "subject_id": document_id,
        "created_at": created_at,
        "details": {
            "mode": "shadow",
            "branch": branch,
            "metadata_score": metadata_score,
            "math_score": math_score,
            "contributing_failures": list(failures),
            "rationale": rationale,
        },
    }


def _live_row(**overrides: Any) -> dict[str, Any]:
    """One live-mode audit row, otherwise identical to shadow."""
    base = _shadow_row(**overrides)
    base["action"] = review.LIVE_ACTION
    base["details"]["mode"] = "live"
    return base


# ----------------------------------------------------------------------
# parse_audit_row — defensive parsing
# ----------------------------------------------------------------------


def test_parse_audit_row_shadow_happy_path() -> None:
    fire = review.parse_audit_row(_shadow_row())
    assert fire is not None
    assert fire.action == review.SHADOW_ACTION
    assert fire.mode == "shadow"
    assert fire.branch == "editor_plus_drift"
    assert fire.metadata_score == 38
    assert fire.math_score == 22
    assert fire.contributing_failures == ("reconciliation_failed_period",)


def test_parse_audit_row_live_happy_path() -> None:
    fire = review.parse_audit_row(_live_row())
    assert fire is not None
    assert fire.action == review.LIVE_ACTION
    assert fire.mode == "live"


def test_parse_audit_row_returns_none_for_non_tampering_action() -> None:
    row = _shadow_row()
    row["action"] = "funder.imported"
    assert review.parse_audit_row(row) is None


def test_parse_audit_row_missing_details_defaults_to_empty() -> None:
    row = {
        "action": review.SHADOW_ACTION,
        "subject_id": "doc-bbb",
        "created_at": "2026-06-10T00:00:00Z",
        # details intentionally absent
    }
    fire = review.parse_audit_row(row)
    assert fire is not None
    assert fire.branch == ""
    assert fire.metadata_score == 0
    assert fire.contributing_failures == ()


def test_parse_audit_row_non_dict_details_does_not_crash() -> None:
    row = _shadow_row()
    row["details"] = "not-a-dict"
    fire = review.parse_audit_row(row)
    assert fire is not None
    assert fire.branch == ""


def test_parse_audit_row_non_list_failures_normalises_to_empty_tuple() -> None:
    row = _shadow_row()
    row["details"]["contributing_failures"] = "not-a-list"
    fire = review.parse_audit_row(row)
    assert fire is not None
    assert fire.contributing_failures == ()


def test_parse_rows_drops_non_tampering_entries() -> None:
    rows = [_shadow_row(), {"action": "funder.imported"}, _live_row()]
    fires = review.parse_rows(rows)
    assert len(fires) == 2
    assert {f.action for f in fires} == set(review.TAMPERING_ACTIONS)


# ----------------------------------------------------------------------
# summarize — aggregation by branch / failure / mode
# ----------------------------------------------------------------------


def test_summarize_empty_input() -> None:
    s = review.summarize([])
    assert s.total_fires == 0
    assert s.distinct_documents == 0
    assert s.by_action == ()
    assert s.by_branch == ()
    assert s.by_failure == ()


def test_summarize_groups_by_branch_descending_by_count() -> None:
    rows = [
        _shadow_row(document_id=f"doc-{i}", branch="editor_plus_drift")
        for i in range(3)
    ] + [
        _shadow_row(document_id=f"doc-{i}", branch="drift_only")
        for i in range(3, 5)
    ]
    fires = review.parse_rows(rows)
    s = review.summarize(fires)
    assert s.total_fires == 5
    assert s.distinct_documents == 5
    assert s.by_branch == (("editor_plus_drift", 3), ("drift_only", 2))


def test_summarize_failure_counter_explodes_per_failure_per_fire() -> None:
    rows = [
        _shadow_row(
            document_id="doc-1",
            failures=("reconciliation_failed_period", "future_dated_period"),
        ),
        _shadow_row(
            document_id="doc-2", failures=("reconciliation_failed_period",)
        ),
    ]
    fires = review.parse_rows(rows)
    s = review.summarize(fires)
    failure_counts = dict(s.by_failure)
    assert failure_counts["reconciliation_failed_period"] == 2
    assert failure_counts["future_dated_period"] == 1


def test_summarize_mode_split_counts_shadow_and_live() -> None:
    fires = review.parse_rows(
        [_shadow_row(document_id="a"), _shadow_row(document_id="b"), _live_row(document_id="c")]
    )
    s = review.summarize(fires)
    assert dict(s.by_mode) == {"shadow": 2, "live": 1}


def test_summarize_distinct_documents_dedupes_repeat_fires() -> None:
    """Two fires on the same document count as ONE distinct doc."""
    fires = review.parse_rows(
        [_shadow_row(document_id="doc-a"), _shadow_row(document_id="doc-a")]
    )
    s = review.summarize(fires)
    assert s.total_fires == 2
    assert s.distinct_documents == 1


def test_summarize_tie_break_is_alphabetical() -> None:
    """Two branches with identical counts sort by key ascending."""
    fires = review.parse_rows(
        [
            _shadow_row(document_id="a", branch="zeta"),
            _shadow_row(document_id="b", branch="alpha"),
        ]
    )
    s = review.summarize(fires)
    assert s.by_branch == (("alpha", 1), ("zeta", 1))


# ----------------------------------------------------------------------
# write_csv — output shape locked
# ----------------------------------------------------------------------


def test_write_csv_header_pinned() -> None:
    buf = io.StringIO()
    review.write_csv([], buf)
    assert buf.getvalue().strip().split(",") == [
        "created_at", "action", "mode", "branch", "document_id",
        "metadata_score", "math_score", "contributing_failures", "rationale",
    ]


def test_write_csv_serialises_failures_with_semicolon() -> None:
    fires = review.parse_rows(
        [_shadow_row(failures=("reconciliation_failed_period", "future_dated_period"))]
    )
    buf = io.StringIO()
    review.write_csv(fires, buf)
    out = buf.getvalue()
    assert "reconciliation_failed_period;future_dated_period" in out


def test_write_csv_one_row_per_fire() -> None:
    fires = review.parse_rows([_shadow_row(document_id=f"d{i}") for i in range(4)])
    buf = io.StringIO()
    review.write_csv(fires, buf)
    # header + 4 rows = 5 non-empty lines.
    non_empty = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(non_empty) == 5


# ----------------------------------------------------------------------
# summary_lines — stderr review block
# ----------------------------------------------------------------------


def test_summary_lines_renders_empty_distribution_as_none() -> None:
    s = review.summarize([])
    lines = review.summary_lines(s)
    text = "\n".join(lines)
    assert "total_fires: 0" in text
    assert "(none)" in text  # empty branches/failures render as (none)


def test_summary_lines_includes_top_branches_and_failures() -> None:
    fires = review.parse_rows(
        [
            _shadow_row(
                document_id="a", branch="editor_plus_drift",
                failures=("reconciliation_failed_period",),
            ),
            _shadow_row(
                document_id="b", branch="editor_plus_drift",
                failures=("reconciliation_failed_period",),
            ),
        ]
    )
    text = "\n".join(review.summary_lines(review.summarize(fires)))
    assert "editor_plus_drift" in text
    assert "reconciliation_failed_period" in text
    assert "total_fires: 2" in text


# ----------------------------------------------------------------------
# fetch_rows — server-side narrowing wiring
# ----------------------------------------------------------------------


class _FakeQuery:
    """Records the chain of method calls so tests can verify the SQL shape."""

    def __init__(self, response_data: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._response_data = response_data

    def _record(self, name: str, *args: Any, **kwargs: Any) -> _FakeQuery:
        self.calls.append((name, args, kwargs))
        return self

    def select(self, *args: Any) -> _FakeQuery:
        return self._record("select", *args)

    def in_(self, column: str, values: list[str]) -> _FakeQuery:
        return self._record("in_", column, values)

    def gte(self, column: str, value: Any) -> _FakeQuery:
        return self._record("gte", column, value)

    def order(self, column: str, *, desc: bool) -> _FakeQuery:
        return self._record("order", column, desc=desc)

    def limit(self, n: int) -> _FakeQuery:
        return self._record("limit", n)

    def execute(self) -> Any:
        class _R:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data
        return _R(self._response_data)


class _FakeClient:
    def __init__(self, response_data: list[dict[str, Any]] | None = None) -> None:
        self.query = _FakeQuery(response_data or [])

    def table(self, name: str) -> _FakeQuery:
        assert name == "audit_log"
        return self.query


def test_fetch_rows_filters_by_tampering_actions_in_clause() -> None:
    client = _FakeClient([])
    review.fetch_rows(client, since=None, limit=100)
    call_names = [c[0] for c in client.query.calls]
    assert call_names == ["select", "in_", "order", "limit"]
    in_call = client.query.calls[1]
    assert in_call[1][0] == "action"
    assert set(in_call[1][1]) == set(review.TAMPERING_ACTIONS)


def test_fetch_rows_adds_gte_when_since_supplied() -> None:
    client = _FakeClient([])
    review.fetch_rows(client, since=date(2026, 6, 1), limit=100)
    call_names = [c[0] for c in client.query.calls]
    assert "gte" in call_names
    gte_call = next(c for c in client.query.calls if c[0] == "gte")
    assert gte_call[1] == ("created_at", "2026-06-01")


def test_fetch_rows_returns_data_list() -> None:
    payload = [_shadow_row(document_id="doc-1")]
    client = _FakeClient(payload)
    out = review.fetch_rows(client, since=None, limit=50)
    assert out == payload


def test_fetch_rows_handles_none_data() -> None:
    """Supabase returns ``data=None`` on empty results in some cases."""

    class _NoneClient:
        def table(self, name: str) -> _FakeQuery:
            q = _FakeQuery([])

            class _NoneExec:
                def __init__(self) -> None:
                    self.data = None

            q.execute = lambda: _NoneExec()  # type: ignore[method-assign]
            return q

    out = review.fetch_rows(_NoneClient(), since=None, limit=50)
    assert out == []


# ----------------------------------------------------------------------
# main — CLI dispatch via DI on _load_client
# ----------------------------------------------------------------------


def test_main_exit_ok_when_no_fires(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(review, "_load_client", lambda: _FakeClient([]))
    rc = review.main([])
    assert rc == review.EXIT_OK
    err = capsys.readouterr().err
    assert "total_fires: 0" in err


def test_main_exit_fires_present_when_rows_returned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        review, "_load_client",
        lambda: _FakeClient([_shadow_row(document_id="doc-1")])
    )
    rc = review.main([])
    assert rc == review.EXIT_FIRES_PRESENT
    captured = capsys.readouterr()
    assert "doc-1" in captured.out  # CSV body
    assert "total_fires: 1" in captured.err  # summary block


def test_main_exit_runtime_error_on_client_init_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> Any:
        raise RuntimeError("no supabase creds")

    monkeypatch.setattr(review, "_load_client", _boom)
    rc = review.main([])
    assert rc == review.EXIT_RUNTIME_ERROR
    assert "could not initialise Supabase client" in capsys.readouterr().err


def test_main_exit_runtime_error_on_query_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _BrokenClient:
        def table(self, name: str) -> Any:
            raise RuntimeError("connection refused")

    monkeypatch.setattr(review, "_load_client", lambda: _BrokenClient())
    rc = review.main([])
    assert rc == review.EXIT_RUNTIME_ERROR
    assert "audit_log query failed" in capsys.readouterr().err


def test_argparse_accepts_since_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _spy(client: Any, *, since: Any, limit: int) -> list[dict[str, Any]]:
        captured["since"] = since
        captured["limit"] = limit
        return []

    monkeypatch.setattr(review, "_load_client", lambda: _FakeClient([]))
    monkeypatch.setattr(review, "fetch_rows", _spy)
    rc = review.main(["--since", "2026-06-01", "--limit", "50"])
    assert rc == review.EXIT_OK
    assert captured["since"] == date(2026, 6, 1)
    assert captured["limit"] == 50


# Sanity touch on the unused stdlib import — guards against test refactors
# silently dropping the Counter import.
def test_internal_counter_import_intact() -> None:
    assert Counter is Counter
