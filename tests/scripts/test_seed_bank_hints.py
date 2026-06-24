"""Smoke tests for ``scripts/seed_bank_hints.py`` (``--bump-parse-count``).

Covers the operator-facing flag added in commit ``c72e28a``:

* DRY-RUN against an existing bank below target — reports the would-set
  intent, does NOT write to the repo or audit log.
* GREATEST-no-op semantics: existing successful_parses ≥ target returns
  action=set with a "no change" detail, without writing.
* --apply against an unknown bank returns action=error and never calls
  the UPDATE / audit paths.
* --apply against an existing bank below target writes one
  ``bank_layouts.successful_parses_bumped`` audit row capturing the
  delta.

No Supabase calls. ``get_supabase`` is monkeypatched to a fake that
captures the UPDATE chain so the apply path is exercised end-to-end
in-memory.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.audit import InMemoryAuditLog  # noqa: E402
from aegis.bank_layouts.models import BankLayoutRow  # noqa: E402
from aegis.bank_layouts.repository import InMemoryBankLayoutRepository  # noqa: E402
from scripts import seed_bank_hints as seed  # noqa: E402


def _seed_bank(
    repo: InMemoryBankLayoutRepository,
    *,
    bank_name: str,
    parses: int,
) -> BankLayoutRow:
    """Pre-seed a bank_layouts row at the requested parse count."""
    now = datetime.now(UTC)
    row = BankLayoutRow(
        bank_name=bank_name,
        layout_fingerprint={},
        successful_parses=parses,
        last_seen=now,
        created_at=now,
    )
    repo._by_lower[bank_name.strip().lower()] = row
    return row


class _FakeSupabaseUpdate:
    """Captures the .table().update().eq().execute() chain.

    Returns a fake execute() result whose ``data`` field carries one
    row mirroring the UPDATE payload — that's enough for
    ``_bump_parse_count_one`` to read ``successful_parses`` off it.
    """

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.match_id: str | None = None
        self._table_name: str | None = None
        self._pending_payload: dict[str, Any] | None = None

    def table(self, name: str) -> _FakeSupabaseUpdate:
        self._table_name = name
        return self

    def update(self, payload: dict[str, Any]) -> _FakeSupabaseUpdate:
        self._pending_payload = payload
        return self

    def eq(self, _column: str, value: str) -> _FakeSupabaseUpdate:
        self.match_id = value
        return self

    def execute(self) -> _FakeSupabaseUpdate:
        if self._pending_payload is None:
            raise AssertionError("execute() called without update payload")
        self.updates.append(dict(self._pending_payload))
        # Mirror the supabase-py response shape: ``data`` is a list of
        # dicts. ``_bump_parse_count_one`` reads ``successful_parses``
        # off the first row.
        self.data = [
            {
                "id": self.match_id,
                "successful_parses": self._pending_payload.get("successful_parses"),
            }
        ]
        # Reset for next call.
        self._pending_payload = None
        return self


# ----------------------------------------------------------------------
# DRY-RUN paths
# ----------------------------------------------------------------------


def test_dry_run_below_target_reports_would_set() -> None:
    """DRY-RUN: existing bank at parses=1, target=3 → would_set + no writes."""
    repo = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()
    _seed_bank(repo, bank_name="Test Bank", parses=1)

    outcome = seed._bump_parse_count_one(
        bank_name="Test Bank",
        target_count=3,
        repo=repo,  # type: ignore[arg-type]
        audit=audit,
        apply_writes=False,
    )

    assert outcome.action == "would_set"
    assert "current=1" in outcome.detail
    assert "DRY-RUN" in outcome.detail
    assert audit.entries == []  # no audit row in dry-run


def test_dry_run_no_change_when_existing_at_target() -> None:
    """DRY-RUN: existing bank at parses=3, target=3 → no-op, action=set."""
    repo = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()
    _seed_bank(repo, bank_name="Test Bank", parses=3)

    outcome = seed._bump_parse_count_one(
        bank_name="Test Bank",
        target_count=3,
        repo=repo,  # type: ignore[arg-type]
        audit=audit,
        apply_writes=False,
    )

    assert outcome.action == "set"
    assert "no change" in outcome.detail
    assert audit.entries == []


def test_dry_run_no_change_when_existing_above_target() -> None:
    """GREATEST(current, target): parses=5 > target=3 → no-op."""
    repo = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()
    _seed_bank(repo, bank_name="Test Bank", parses=5)

    outcome = seed._bump_parse_count_one(
        bank_name="Test Bank",
        target_count=3,
        repo=repo,  # type: ignore[arg-type]
        audit=audit,
        apply_writes=True,
    )

    assert outcome.action == "set"
    assert "no change" in outcome.detail
    # No audit row because no UPDATE happened.
    assert audit.entries == []


# ----------------------------------------------------------------------
# --apply paths
# ----------------------------------------------------------------------


def test_apply_unknown_bank_returns_error() -> None:
    """--apply with no matching row → action=error, no UPDATE / no audit."""
    repo = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()

    outcome = seed._bump_parse_count_one(
        bank_name="Unknown Bank",
        target_count=3,
        repo=repo,  # type: ignore[arg-type]
        audit=audit,
        apply_writes=True,
    )

    assert outcome.action == "error"
    assert "no bank_layouts row" in outcome.detail
    assert audit.entries == []


def test_apply_writes_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """--apply with bank at parses=1, target=3 → UPDATE + one audit row."""
    repo = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()
    row = _seed_bank(repo, bank_name="Test Bank", parses=1)

    fake_sb = _FakeSupabaseUpdate()
    monkeypatch.setattr(seed, "get_supabase", lambda: fake_sb)

    outcome = seed._bump_parse_count_one(
        bank_name="Test Bank",
        target_count=3,
        repo=repo,  # type: ignore[arg-type]
        audit=audit,
        apply_writes=True,
    )

    # Outcome reflects the bump.
    assert outcome.action == "set"
    assert "bumped successful_parses 1 → 3" in outcome.detail
    # UPDATE was issued for this row.
    assert len(fake_sb.updates) == 1
    assert fake_sb.updates[0] == {"successful_parses": 3}
    assert fake_sb.match_id == str(row.id)
    # One audit row, correct action + delta + threshold context.
    assert len(audit.entries) == 1
    a = audit.entries[0]
    assert a["actor"] == "seed_bank_hints_script"
    assert a["action"] == "bank_layouts.successful_parses_bumped"
    assert a["subject_type"] == "bank_layout"
    # subject_id is serialised to str on InMemoryAuditLog.record;
    # check it round-trips as a valid UUID rather than depending on
    # the row's specific id value (BankLayoutRow auto-generates).
    assert UUID(a["subject_id"]) == row.id
    # bank_name is in the central PII mask set per CLAUDE.md, so the
    # InMemoryAuditLog (and the Supabase-backed one) writes '***' to
    # the audit row. The non-PII delta + threshold fields stay legible.
    assert a["details"]["bank_name"] == "***"
    assert a["details"]["previous_successful_parses"] == 1
    assert a["details"]["new_successful_parses"] == 3
    assert a["details"]["target_count"] == 3
    assert "operator-authorized backfill" in a["details"]["note"]


def test_apply_with_uppercase_match_ignores_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``find_by_bank_name`` is case-insensitive — a bank seeded as
    'Test Bank' is reachable as 'TEST BANK' from the operator's CLI
    arg without falling through to the 'no row' branch."""
    repo = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()
    _seed_bank(repo, bank_name="Test Bank", parses=2)

    fake_sb = _FakeSupabaseUpdate()
    monkeypatch.setattr(seed, "get_supabase", lambda: fake_sb)

    outcome = seed._bump_parse_count_one(
        bank_name="TEST BANK",
        target_count=3,
        repo=repo,  # type: ignore[arg-type]
        audit=audit,
        apply_writes=True,
    )

    assert outcome.action == "set"
    assert "bumped successful_parses 2 → 3" in outcome.detail
    assert len(audit.entries) == 1


# Quiet a noisy `uuid4` import on test discovery — the helper above
# constructs UUIDs implicitly via BankLayoutRow's default factory, but
# the import-site warning hits without an explicit reference.
_ = uuid4
