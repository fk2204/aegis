"""Structural tests for migration 073 — evolutionary ALTER on the
existing ``processor_statements`` table (migration 020).

Mirrors the ``tests/test_migrations_033.py`` + ``tests/test_migration_074.py``
patterns. No live SQL: file-shape regex assertions catch typos / drift
before the migration hits the runner.

Migration 073's job is to evolve 020's table in place (not re-create it).
The test set therefore asserts the ALTER shape: widened CHECK on
``processor``, four ADD COLUMN statements, a UNIQUE constraint on
``document_id``, DROP NOT NULL on the columns the new dossier-shape
write path doesn't carry, and the audit row that records the change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR: Final[Path] = _REPO_ROOT / "migrations"
_MIGRATION_FILENAME: Final[str] = "073_processor_statements.sql"


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


def test_apply_migrations_knows_about_073() -> None:
    """The runner's MIGRATION_PROBES dict must register the 073 entry."""
    from scripts.apply_migrations import MIGRATION_PROBES

    assert _MIGRATION_FILENAME in MIGRATION_PROBES
    probe = MIGRATION_PROBES[_MIGRATION_FILENAME]
    assert "processor_statements" in probe


# ---------------------------------------------------------------------------
# 073 is an ALTER on the existing 020 table, NOT a CREATE
# ---------------------------------------------------------------------------


def test_migration_does_not_recreate_table() -> None:
    """073 must NOT re-CREATE the processor_statements table. A first
    draft used CREATE TABLE IF NOT EXISTS with a different column set,
    which is a production-breaking no-op (IF NOT EXISTS sees the 020
    table and skips; the new repo would then write column names that
    don't exist on the live shape). Forbidding the CREATE keyword
    enforces the ALTER-only contract."""
    sql = _read()
    assert not re.search(
        r"CREATE\s+TABLE",
        sql,
        re.IGNORECASE,
    ), "073 must ALTER processor_statements, not CREATE it (020 already creates it)"


# ---------------------------------------------------------------------------
# Widened CHECK on processor — adds toast / clover / paypal
# ---------------------------------------------------------------------------


def test_drops_old_processor_check_constraint() -> None:
    """Postgres has no ALTER CONSTRAINT for CHECK widening — must DROP
    then ADD. 073 wraps the DROP in a DO block + IF EXISTS guard so
    re-applying is idempotent."""
    sql = _read()
    assert re.search(
        r"DROP CONSTRAINT\s+processor_statements_processor_check",
        sql,
        re.IGNORECASE,
    )


def test_adds_widened_processor_check_constraint() -> None:
    """The replacement constraint must accept the five processors the
    repository's ``ProcessorType`` Literal carries. Stripe + Square
    were the original two on migration 020; Toast / Clover / PayPal
    are pre-allocated for parsers that haven't landed yet."""
    sql = _read()
    pattern = (
        r"ADD CONSTRAINT\s+processor_statements_processor_check"
        r"\s*\n?\s*CHECK\s*\(\s*processor\s+IN"
    )
    assert re.search(pattern, sql, re.IGNORECASE)
    for value in ("stripe", "square", "toast", "clover", "paypal"):
        assert f"'{value}'" in sql, f"processor value {value!r} missing from widened CHECK"


# ---------------------------------------------------------------------------
# ADD COLUMN statements for the four new dossier-shape columns
# ---------------------------------------------------------------------------


def test_adds_avg_daily_volume_column() -> None:
    """avg_daily_volume NUMERIC(14,2) — CLAUDE.md money rule: never
    float8. Matches the bank-statement analyses convention."""
    sql = _read()
    assert re.search(
        r"ADD COLUMN IF NOT EXISTS\s+avg_daily_volume\s+NUMERIC\(14,\s*2\)",
        sql,
        re.IGNORECASE,
    )


def test_adds_refund_rate_column() -> None:
    """refund_rate NUMERIC(6,4) — rate math, four decimal precision."""
    sql = _read()
    assert re.search(
        r"ADD COLUMN IF NOT EXISTS\s+refund_rate\s+NUMERIC\(6,\s*4\)",
        sql,
        re.IGNORECASE,
    )


def test_adds_parse_method_column() -> None:
    """parse_method TEXT — discriminates csv / pdf_vision so the
    dossier can label the parse path."""
    sql = _read()
    assert re.search(
        r"ADD COLUMN IF NOT EXISTS\s+parse_method\s+TEXT",
        sql,
        re.IGNORECASE,
    )


def test_adds_raw_line_items_column() -> None:
    """raw_line_items JSONB — forensic-replay payload; lets the
    operator drill into per-row dumps without re-running extraction."""
    sql = _read()
    assert re.search(
        r"ADD COLUMN IF NOT EXISTS\s+raw_line_items\s+JSONB",
        sql,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# UNIQUE constraint on document_id (backs the upsert ON CONFLICT path)
# ---------------------------------------------------------------------------


def test_adds_unique_constraint_on_document_id() -> None:
    """UNIQUE(document_id) is the load-bearing constraint for the
    SupabaseProcessorStatementRepository upsert — without it, re-parses
    would insert duplicate rows instead of replacing."""
    sql = _read()
    assert re.search(
        r"ADD CONSTRAINT\s+processor_statements_document_id_key\s+UNIQUE\s*\(\s*document_id\s*\)",
        sql,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# DROP NOT NULL on the columns the dossier-shape write path doesn't carry
# ---------------------------------------------------------------------------


def test_drops_not_null_on_period_dates() -> None:
    sql = _read()
    assert re.search(
        r"ALTER COLUMN\s+period_start\s+DROP NOT NULL",
        sql,
        re.IGNORECASE,
    )
    assert re.search(
        r"ALTER COLUMN\s+period_end\s+DROP NOT NULL",
        sql,
        re.IGNORECASE,
    )


def test_drops_not_null_on_legacy_money_columns() -> None:
    """020 required gross_volume / refunds_total / chargebacks_total /
    fees_total / payouts_total / net_revenue. The dossier-shape write
    path doesn't carry refunds_total / chargebacks_total today; the
    others get mapped via the repo encoder. Dropping NOT NULL keeps
    the migration non-destructive while letting the new path land
    rows that populate only the dossier-relevant subset."""
    sql = _read()
    for column in (
        "gross_volume",
        "refunds_total",
        "chargebacks_total",
        "fees_total",
        "payouts_total",
        "net_revenue",
    ):
        assert re.search(
            rf"ALTER COLUMN\s+{column}\s+DROP NOT NULL",
            sql,
            re.IGNORECASE,
        ), f"DROP NOT NULL missing for column {column!r}"


def test_drops_not_null_on_validation_columns() -> None:
    """validation_passed + parse_status were NOT NULL on 020. The new
    write path only upserts on successful parses (the worker check
    gates this), so these are implicitly true / proceed. Left nullable
    so a follow-up that backfills the validator outcome can populate
    them without a second ALTER."""
    sql = _read()
    assert re.search(
        r"ALTER COLUMN\s+validation_passed\s+DROP NOT NULL",
        sql,
        re.IGNORECASE,
    )
    assert re.search(
        r"ALTER COLUMN\s+parse_status\s+DROP NOT NULL",
        sql,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


def test_migration_wraps_in_transaction() -> None:
    sql = _read()
    assert sql.upper().count("BEGIN") >= 1
    assert sql.upper().count("COMMIT") >= 1


def test_migration_writes_audit_row() -> None:
    """Phase 2 acceptance: every state change writes to audit_log. The
    schema-alter itself is a state change."""
    sql = _read()
    assert re.search(r"INSERT INTO audit_log", sql, re.IGNORECASE)
    assert "processor_statements.schema_altered" in sql
    assert "migration_073" in sql
    # Audit payload should reference the base migration so the audit
    # trail makes the 020 → 073 evolution explicit.
    assert "020_processor_statements.sql" in sql
