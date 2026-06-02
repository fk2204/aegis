"""Structural tests for migration 033 + its db_verify probes.

Chunk A of the PDF retention redesign. These tests do NOT execute
SQL — they check that the migration file exists, the apply_migrations
runner knows about it (MIGRATION_PROBES entry), and the three
db_check SQL files are present with the expected EXPECT_ROWS headers.
The actual migration body runs against prod via the runner; this
catches dev-time mistakes (forgot to register the probe, mistyped
the filename, anomaly check missing) before they reach the deploy
chain.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR: Final[Path] = _REPO_ROOT / "migrations"
_DB_CHECKS_DIR: Final[Path] = _REPO_ROOT / "scripts" / "db_checks"
_MIGRATION_FILENAME: Final[str] = "033_documents_storage_and_retention.sql"


# ---------------------------------------------------------------------------
# Migration file shape
# ---------------------------------------------------------------------------


def test_migration_033_file_exists() -> None:
    path = _MIGRATIONS_DIR / _MIGRATION_FILENAME
    assert path.exists(), f"missing migration file: {path}"


def test_migration_033_adds_all_four_document_columns() -> None:
    """The DDL must add all four PDF-retention columns. Worker (chunk B)
    + view route (chunk C) + retention sweep (chunk E) all assume these
    are present."""
    body = (_MIGRATIONS_DIR / _MIGRATION_FILENAME).read_text(encoding="utf-8")
    for column in (
        "storage_path",
        "sha256_original",
        "encryption_key_version",
        "retention_until",
    ):
        assert column in body, f"migration 033 missing column {column}"


def test_migration_033_adds_merchants_deleted_at() -> None:
    """The Q1 = Option A decision adds the soft-delete column to
    merchants in the same migration. Worker (chunk B) extender hangs
    off this column."""
    body = (_MIGRATIONS_DIR / _MIGRATION_FILENAME).read_text(encoding="utf-8")
    assert "deleted_at" in body
    assert "ALTER TABLE public.merchants" in body


def test_migration_033_uses_idempotent_add_column() -> None:
    """``ADD COLUMN IF NOT EXISTS`` keeps the migration safe under
    accidental double-apply (defense against drift in
    MigrationDriftError edge cases)."""
    body = (_MIGRATIONS_DIR / _MIGRATION_FILENAME).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS" in body


def test_migration_033_creates_partial_indexes_with_predicate() -> None:
    """Both indexes are partial (only index rows that matter), so the
    nightly retention sweep doesn't scan legacy / already-swept docs.
    The probe at scripts/db_checks/migration-033-indexes-exist.sql
    relies on the predicate being present."""
    body = (_MIGRATIONS_DIR / _MIGRATION_FILENAME).read_text(encoding="utf-8")
    assert "WHERE storage_path IS NOT NULL" in body
    assert "WHERE deleted_at IS NOT NULL" in body


def test_migration_033_wraps_in_transaction() -> None:
    """Atomicity: either all five new columns + two indexes land
    together, or none do. Prevents a half-applied schema after a
    runner crash mid-migration."""
    body = (_MIGRATIONS_DIR / _MIGRATION_FILENAME).read_text(encoding="utf-8")
    assert body.upper().count("BEGIN") >= 1
    assert body.upper().count("COMMIT") >= 1


# ---------------------------------------------------------------------------
# MIGRATION_PROBES registration
# ---------------------------------------------------------------------------


def test_apply_migrations_knows_about_033() -> None:
    """The runner's MIGRATION_PROBES dict must have the 033 entry so
    bootstrap detection works correctly (a fresh DB after migration
    033 has applied gets recognized via the probe instead of being
    re-applied)."""
    from scripts.apply_migrations import MIGRATION_PROBES

    assert _MIGRATION_FILENAME in MIGRATION_PROBES
    probe = MIGRATION_PROBES[_MIGRATION_FILENAME]
    assert "storage_path" in probe
    assert "documents" in probe


# ---------------------------------------------------------------------------
# db_check SQL files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("check_name", [
    "migration-033-columns-exist",
    "migration-033-indexes-exist",
    "migration-033-no-retained-forever-anomaly",
])
def test_db_check_file_exists(check_name: str) -> None:
    """Each db_check SQL file lands at the expected path so
    ``scripts/db_verify.py --check <name>`` resolves correctly."""
    path = _DB_CHECKS_DIR / f"{check_name}.sql"
    assert path.exists(), f"missing db_check file: {path}"


def test_columns_exist_check_has_expect_rows_5() -> None:
    """The probe asserts 4 documents columns + 1 merchants column =
    exactly 5 rows from the information_schema query. EXPECT_ROWS is
    the load-bearing assertion — without it the check would pass on
    a partial migration."""
    path = _DB_CHECKS_DIR / "migration-033-columns-exist.sql"
    body = path.read_text(encoding="utf-8")
    assert "EXPECT_ROWS: 5" in body


def test_indexes_exist_check_has_expect_rows_2() -> None:
    """Two indexes — one per partial index added by the migration."""
    path = _DB_CHECKS_DIR / "migration-033-indexes-exist.sql"
    body = path.read_text(encoding="utf-8")
    assert "EXPECT_ROWS: 2" in body


def test_anomaly_check_has_expect_rows_0() -> None:
    """The "retained forever" anomaly check returns zero rows on a
    healthy database — any row with storage_path set but
    retention_until NULL is a bug to investigate."""
    path = _DB_CHECKS_DIR / "migration-033-no-retained-forever-anomaly.sql"
    body = path.read_text(encoding="utf-8")
    assert "EXPECT_ROWS: 0" in body
    # The SQL itself must select the anomaly shape
    assert "storage_path IS NOT NULL" in body
    assert "retention_until IS NULL" in body
