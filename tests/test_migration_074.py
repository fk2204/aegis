"""Structural tests for migration 074 — deal_outcomes + weight_calibration_log.

Mirrors the ``tests/test_migrations_033.py`` + ``tests/compliance/test_migrations.py``
patterns. No live SQL: file-shape regex assertions catch typos / drift
before the migration hits the runner.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR: Final[Path] = _REPO_ROOT / "migrations"
_MIGRATION_FILENAME: Final[str] = "074_deal_outcomes_and_weight_calibration.sql"


def _read() -> str:
    path = _MIGRATIONS_DIR / _MIGRATION_FILENAME
    assert path.exists(), f"missing migration file: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File presence + registration
# ---------------------------------------------------------------------------


def test_migration_file_exists() -> None:
    path = _MIGRATIONS_DIR / _MIGRATION_FILENAME
    assert path.exists(), f"missing migration file: {path}"


def test_apply_migrations_knows_about_074() -> None:
    """The runner's MIGRATION_PROBES dict must register the 074 entry so
    bootstrap detection skips re-applying an already-deployed migration."""
    from scripts.apply_migrations import MIGRATION_PROBES

    assert _MIGRATION_FILENAME in MIGRATION_PROBES
    probe = MIGRATION_PROBES[_MIGRATION_FILENAME]
    assert "deal_outcomes" in probe


# ---------------------------------------------------------------------------
# deal_outcomes table shape
# ---------------------------------------------------------------------------


def test_creates_deal_outcomes_table() -> None:
    sql = _read()
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+deal_outcomes",
        sql,
        re.IGNORECASE,
    )


def test_deal_outcomes_has_all_required_columns() -> None:
    """Every column the calibration engine + record route reads / writes."""
    sql = _read()
    for column in (
        "id",
        "merchant_id",
        "decision_id",
        "submitted_at",
        "funder_id",
        "funder_decision",
        "funded_amount",
        "factor_rate",
        "term_days",
        "first_payment_date",
        "outcome",
        "outcome_recorded_at",
        "charge_off_amount",
        "notes",
        "created_at",
        "created_by",
    ):
        assert re.search(rf"^\s*{column}\b", sql, re.IGNORECASE | re.MULTILINE), (
            f"column {column!r} missing in 073 migration"
        )


def test_deal_outcomes_decision_id_references_decisions() -> None:
    """The FK is the immutability contract between outcomes and the
    immutable decisions table (migration 015 + 070)."""
    sql = _read()
    assert re.search(
        r"decision_id\s+UUID\s+NOT NULL\s+REFERENCES\s+decisions\s*\(\s*id\s*\)",
        sql,
        re.IGNORECASE,
    )


def test_deal_outcomes_funder_decision_check_constraint() -> None:
    """The CHECK accepts exactly the three funder_decision enum values
    the calibration engine + record route validate against."""
    sql = _read()
    for value in ("approved", "declined", "countered"):
        assert f"'{value}'" in sql, f"funder_decision value {value!r} missing"
    assert re.search(
        r"funder_decision\s+TEXT\s+NOT NULL\s*\n\s*CHECK\s*\(\s*funder_decision\s+IN",
        sql,
        re.IGNORECASE,
    )


def test_deal_outcomes_outcome_check_constraint_full_enum() -> None:
    """The CHECK MUST accept charged_off + paid_in_full — those are the
    two ends of the calibration engine's empirical comparison."""
    sql = _read()
    for value in (
        "paying",
        "paid_in_full",
        "charged_off",
        "defaulted",
        "renewed",
        "pending",
    ):
        assert f"'{value}'" in sql, f"outcome value {value!r} missing from CHECK"


def test_deal_outcomes_money_columns_use_numeric() -> None:
    """CLAUDE.md money rule: never float8. ``numeric(12,2)`` matches the
    other money columns in this codebase."""
    sql = _read()
    for column in ("funded_amount", "charge_off_amount"):
        assert re.search(
            rf"{column}\s+NUMERIC\(12,\s*2\)",
            sql,
            re.IGNORECASE,
        ), f"{column} must be NUMERIC(12,2)"
    assert re.search(r"factor_rate\s+NUMERIC\(6,\s*4\)", sql, re.IGNORECASE)


def test_deal_outcomes_indexes_exist() -> None:
    sql = _read()
    for index_pattern in (
        r"CREATE INDEX[^;]*ON\s+deal_outcomes\s*\(\s*merchant_id",
        r"CREATE INDEX[^;]*ON\s+deal_outcomes\s*\(\s*decision_id",
        r"CREATE INDEX[^;]*ON\s+deal_outcomes\s*\(\s*outcome",
    ):
        assert re.search(index_pattern, sql, re.IGNORECASE), (
            f"missing index pattern: {index_pattern}"
        )


def test_deal_outcomes_outcome_partial_index_excludes_pending() -> None:
    """The partial index on outcome filters out pending rows — they
    have no signal until the deal reaches terminal state."""
    sql = _read()
    assert re.search(
        r"CREATE INDEX[^;]*ON\s+deal_outcomes\s*\(\s*outcome\s*\)[\s\S]*?"
        r"WHERE\s+outcome\s*!=\s*'pending'",
        sql,
        re.IGNORECASE,
    )


def test_deal_outcomes_enables_row_level_security() -> None:
    sql = _read()
    assert re.search(
        r"ALTER TABLE\s+deal_outcomes\s+ENABLE ROW LEVEL SECURITY",
        sql,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# weight_calibration_log table shape
# ---------------------------------------------------------------------------


def test_creates_weight_calibration_log_table() -> None:
    sql = _read()
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+weight_calibration_log",
        sql,
        re.IGNORECASE,
    )


def test_weight_calibration_log_has_required_columns() -> None:
    sql = _read()
    for column in (
        "id",
        "flag_code",
        "suggested_weight",
        "current_weight",
        "operator_decision",
        "operator_notes",
        "sample_size",
        "confidence",
        "reviewed_at",
        "reviewed_by",
    ):
        assert re.search(rf"^\s*{column}\b", sql, re.IGNORECASE | re.MULTILINE), (
            f"column {column!r} missing in weight_calibration_log"
        )


def test_weight_calibration_log_operator_decision_check() -> None:
    sql = _read()
    for value in ("accepted", "rejected", "deferred"):
        assert f"'{value}'" in sql, f"operator_decision value {value!r} missing"


def test_weight_calibration_log_confidence_check_enum() -> None:
    sql = _read()
    for value in ("low", "medium", "high"):
        assert f"'{value}'" in sql, f"confidence value {value!r} missing"


def test_weight_calibration_log_enables_row_level_security() -> None:
    sql = _read()
    assert re.search(
        r"ALTER TABLE\s+weight_calibration_log\s+ENABLE ROW LEVEL SECURITY",
        sql,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


def test_migration_wraps_in_transaction() -> None:
    """Atomicity: both tables + indexes + audit row land together, or
    none do. Prevents a half-applied schema after a runner crash."""
    sql = _read()
    assert sql.upper().count("BEGIN") >= 1
    assert sql.upper().count("COMMIT") >= 1


def test_migration_writes_audit_row() -> None:
    """Phase 2 acceptance: every state change writes to audit_log. The
    schema-add itself is a state change."""
    sql = _read()
    assert re.search(r"INSERT INTO audit_log", sql, re.IGNORECASE)
    assert "outcome_feedback_loop.schema_added" in sql
    assert "migration_074" in sql
