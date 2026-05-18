"""Tests for scripts/backfill_decisions (mp Phase 2).

We don't have a live Supabase in the test env. The supabase-py client is
chained (``.table(...).select(...).in_(...).execute()`` /
``.upsert(...).execute()``); we stand up a small fake that records the
calls the backfill makes and lets us seed the rows ``execute()`` returns.

What this covers:
- _build_row maps parse_status → decision correctly.
- Documents missing state or parsed_at are skipped (counts.skipped increments).
- Backfill rows carry the right defensive defaults: aegis_version='backfill',
  rule_pack_version='pre-snapshot-table', decided_by='backfill_2026_05',
  cfdl_tier=3, backfill_quality='minimal'.
- decided_at echoes the original documents.parsed_at — NOT now().
- dry-run writes nothing but reports the would-write count.
- The script is idempotent at the API level (it calls upsert with
  on_conflict='deal_id' + ignore_duplicates=True).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from scripts import backfill_decisions

# ---------------------------------------------------------------------------
# Tiny supabase fake — supports the chainable surface the script uses.
# ---------------------------------------------------------------------------


class _FakeQuery:
    """Records calls and returns canned rows on .execute()."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def select(self, *args: Any) -> _FakeQuery:
        self.calls.append(("select", args))
        return self

    def in_(self, *args: Any) -> _FakeQuery:
        self.calls.append(("in_", args))
        return self

    def execute(self) -> Any:
        return type("R", (), {"data": list(self._rows)})()


class _FakeUpsert:
    def __init__(self, captured: list[dict[str, Any]], kwargs: dict[str, Any]) -> None:
        self.captured = captured
        self.kwargs = kwargs

    def execute(self) -> Any:
        return type("R", (), {"data": list(self.captured)})()


class _FakeTable:
    def __init__(self, name: str, rows: list[dict[str, Any]]) -> None:
        self.name = name
        self.rows = rows
        self.upserted: list[dict[str, Any]] = []
        self.upsert_kwargs: dict[str, Any] = {}

    def select(self, *args: Any) -> _FakeQuery:
        q = _FakeQuery(self.rows)
        return q.select(*args)

    def upsert(self, rows: list[dict[str, Any]], **kwargs: Any) -> _FakeUpsert:
        self.upserted.extend(rows)
        self.upsert_kwargs = kwargs
        return _FakeUpsert(rows, kwargs)


class _FakeSupabase:
    def __init__(self, *, documents: list[dict[str, Any]]) -> None:
        self._documents = documents
        self.tables: dict[str, _FakeTable] = {}

    def table(self, name: str) -> _FakeTable:
        if name not in self.tables:
            rows = self._documents if name == "documents" else []
            self.tables[name] = _FakeTable(name, rows)
        return self.tables[name]


# ---------------------------------------------------------------------------
# _build_row — pure mapping tests
# ---------------------------------------------------------------------------


def _doc(
    *,
    parse_status: str = "proceed",
    state: str | None = "CA",
    parsed_at: str | None = "2026-03-14T10:30:00+00:00",
    analyses_present: bool = True,
) -> dict[str, Any]:
    analyses: list[dict[str, Any]] = (
        [{"id": str(uuid4())}] if analyses_present else []
    )
    return {
        "id": str(uuid4()),
        "parse_status": parse_status,
        "parsed_at": parsed_at,
        "merchants": {"id": str(uuid4()), "state": state} if state else None,
        "analyses": analyses,
    }


def test_build_row_proceed_maps_to_approve() -> None:
    row = backfill_decisions._build_row(_doc(parse_status="proceed"))
    assert row is not None
    assert row["decision"] == "approve"
    assert row["decided_by"] == "backfill_2026_05"
    assert row["aegis_version"] == "backfill"
    assert row["rule_pack_version"] == "pre-snapshot-table"
    assert row["backfill_quality"] == "minimal"
    assert row["cfdl_tier"] == 3  # defensive default per the script's docstring
    assert row["state_code"] == "CA"


def test_build_row_review_maps_to_manual_review() -> None:
    row = backfill_decisions._build_row(_doc(parse_status="review"))
    assert row is not None
    assert row["decision"] == "manual_review"


def test_build_row_manual_review_maps_to_manual_review() -> None:
    row = backfill_decisions._build_row(_doc(parse_status="manual_review"))
    assert row is not None
    assert row["decision"] == "manual_review"


def test_build_row_skips_missing_state() -> None:
    assert backfill_decisions._build_row(_doc(state=None)) is None


def test_build_row_skips_missing_parsed_at() -> None:
    assert backfill_decisions._build_row(_doc(parsed_at=None)) is None


def test_build_row_uses_original_parsed_at_not_now() -> None:
    """Master plan §2 principle 3: backfill must echo the original
    decision timestamp, never NOW(). The whole point of the snapshot
    table is to preserve when the decision was made."""
    fixed_ts = "2025-03-14T10:30:00+00:00"
    row = backfill_decisions._build_row(_doc(parsed_at=fixed_ts))
    assert row is not None
    assert row["decided_at"] == fixed_ts


def test_build_row_uppercases_state() -> None:
    row = backfill_decisions._build_row(_doc(state="ca"))
    assert row is not None
    assert row["state_code"] == "CA"


def test_build_row_links_analysis_id_when_present() -> None:
    doc = _doc(analyses_present=True)
    row = backfill_decisions._build_row(doc)
    assert row is not None
    assert row["analysis_id"] == doc["analyses"][0]["id"]


def test_build_row_analysis_id_none_when_no_analyses() -> None:
    row = backfill_decisions._build_row(_doc(analyses_present=False))
    assert row is not None
    assert row["analysis_id"] is None


def test_build_row_skips_unmappable_status() -> None:
    """Documents with parse_status not in the mapping (pending/error)
    return None. In practice the SQL .in_() filter already drops these
    rows before they reach _build_row; this guards the function-level
    contract too."""
    assert backfill_decisions._build_row(_doc(parse_status="pending")) is None


def test_build_row_score_factors_left_empty() -> None:
    """Per master plan §2 principle 3 + refinement (4): score_factors
    is NOT re-derived by re-running the scorer. Snapshot the past, don't
    fabricate it."""
    row = backfill_decisions._build_row(_doc())
    assert row is not None
    assert row["score_factors"] == {}
    assert row["score"] is None
    assert row["contributing_transaction_uuids"] == []
    assert row["bank_statement_pdf_sha256"] is None


# ---------------------------------------------------------------------------
# backfill() — end-to-end against the supabase fake
# ---------------------------------------------------------------------------


def _install_fake(
    monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]]
) -> _FakeSupabase:
    fake = _FakeSupabase(documents=docs)
    monkeypatch.setattr(backfill_decisions, "get_supabase", lambda: fake)
    return fake


def test_backfill_writes_rows_for_eligible_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = [_doc(parse_status="proceed"), _doc(parse_status="review")]
    fake = _install_fake(monkeypatch, docs)

    counts = backfill_decisions.backfill(dry_run=False)
    assert counts == {"candidates": 2, "written": 2, "skipped": 0}

    table = fake.tables["decisions"]
    assert len(table.upserted) == 2
    # Both rows carry the backfill markers.
    for r in table.upserted:
        assert r["decided_by"] == "backfill_2026_05"
        assert r["aegis_version"] == "backfill"
        assert r["rule_pack_version"] == "pre-snapshot-table"
        assert r["backfill_quality"] == "minimal"


def test_backfill_passes_idempotency_options_to_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upsert must be configured to ignore conflicts on the partial
    unique index ``uq_decisions_backfill_per_deal``. Without these
    kwargs, re-running would raise duplicate-key errors."""
    fake = _install_fake(monkeypatch, [_doc()])
    backfill_decisions.backfill(dry_run=False)
    kwargs = fake.tables["decisions"].upsert_kwargs
    assert kwargs.get("on_conflict") == "deal_id"
    assert kwargs.get("ignore_duplicates") is True


def test_backfill_skips_documents_missing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake(
        monkeypatch,
        [_doc(parse_status="proceed", state=None), _doc(parse_status="proceed")],
    )
    counts = backfill_decisions.backfill(dry_run=False)
    assert counts == {"candidates": 2, "written": 1, "skipped": 1}
    assert len(fake.tables["decisions"].upserted) == 1


def test_backfill_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake(monkeypatch, [_doc(), _doc()])
    counts = backfill_decisions.backfill(dry_run=True)
    assert counts == {"candidates": 2, "written": 0, "skipped": 0}
    # decisions table never touched during dry-run.
    assert "decisions" not in fake.tables


def test_backfill_no_eligible_documents_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every candidate is skipped (missing state) we must NOT call
    upsert([]) on the decisions table — supabase-py rejects empty
    upserts, and the script already guards this."""
    fake = _install_fake(monkeypatch, [_doc(state=None)])
    counts = backfill_decisions.backfill(dry_run=False)
    assert counts["written"] == 0
    assert counts["skipped"] == 1
    assert "decisions" not in fake.tables


def test_backfill_preserves_original_decision_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the snapshot table: the row reflects when the
    decision was made, never NOW()."""
    parsed_at = datetime(2025, 3, 14, 10, 30, tzinfo=UTC).isoformat()
    fake = _install_fake(
        monkeypatch, [_doc(parse_status="proceed", parsed_at=parsed_at)]
    )
    backfill_decisions.backfill(dry_run=False)
    row = fake.tables["decisions"].upserted[0]
    assert row["decided_at"] == parsed_at
