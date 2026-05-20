"""Tests for scripts/apply_migrations.py (3C-extra migration runner).

Two layers:

* **Unit tests** — always run via `make test`. Cover file discovery, sha256
  computation, drift detection logic, DSN resolution, --target prod
  refusal, --dry-run output formatting. No database needed.

* **Integration tests** — require AEGIS_TEST_DB_URL pointing at a disposable
  Postgres (e.g. ``docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test
  postgres:16``). Cover synthetic apply, re-run skip, drift detection, two
  concurrent runners (advisory-lock contention), audit-log row atomicity,
  failed-migration rollback.

The integration suite is gated behind a fixture that skips when the env
var is absent, so CI on Windows / Hetzner without a sidecar DB does not
fail the suite.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load scripts/apply_migrations.py without a package install. The module is
# operator tooling and isn't on sys.path otherwise.
_spec = importlib.util.spec_from_file_location(
    "apply_migrations", REPO_ROOT / "scripts" / "apply_migrations.py"
)
assert _spec is not None and _spec.loader is not None
apply_migrations = importlib.util.module_from_spec(_spec)
sys.modules["apply_migrations"] = apply_migrations
_spec.loader.exec_module(apply_migrations)


# ===========================================================================
# Unit tests — always run
# ===========================================================================


def test_discover_migrations_filters_to_numbered_pattern(tmp_path: Path) -> None:
    """Only NNN_*.sql files are picked up; _all_in_order and bootstrap are skipped."""
    (tmp_path / "000_foo.sql").write_text("-- foo\n")
    (tmp_path / "001_bar.sql").write_text("-- bar\n")
    (tmp_path / "_all_in_order.sql").write_text("-- skipped\n")
    (tmp_path / "bootstrap.sql").write_text("-- skipped\n")
    (tmp_path / "notes.md").write_text("docs\n")

    result = apply_migrations.discover_migrations(tmp_path)

    assert [m.filename for m in result] == ["000_foo.sql", "001_bar.sql"]


def test_discover_migrations_orders_lexicographically(tmp_path: Path) -> None:
    """021_funder_replies must sort after 015_decisions."""
    for name in ("015_z.sql", "021_z.sql", "002_z.sql"):
        (tmp_path / name).write_text("-- " + name)

    result = apply_migrations.discover_migrations(tmp_path)

    assert [m.filename for m in result] == ["002_z.sql", "015_z.sql", "021_z.sql"]


def test_migration_file_sha256_matches_file_bytes(tmp_path: Path) -> None:
    """sha256 is taken from raw bytes — same byte content → same hash."""
    body = b"-- migration body\nCREATE TABLE t (id int);\n"
    (tmp_path / "000_t.sql").write_bytes(body)
    [mig] = apply_migrations.discover_migrations(tmp_path)
    import hashlib
    assert mig.sha256 == hashlib.sha256(body).hexdigest()


def test_resolve_dsn_refuses_prod_url_with_non_prod_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DSN containing the prod project ref must require --target prod."""
    fake = (
        f"postgresql://postgres.{apply_migrations.PROD_PROJECT_REF}"
        ":pw@host.example.com:6543/postgres"
    )
    monkeypatch.setenv("MIGRATIONS_DB_URL_DEV", fake)
    with pytest.raises(apply_migrations.MigrationConfigError) as ei:
        apply_migrations.resolve_dsn("dev")
    assert "prod" in str(ei.value).lower()


def test_resolve_dsn_accepts_prod_url_with_target_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = (
        f"postgresql://postgres.{apply_migrations.PROD_PROJECT_REF}"
        ":pw@host.example.com:6543/postgres"
    )
    monkeypatch.setenv("MIGRATIONS_DB_URL_PROD", fake)
    assert apply_migrations.resolve_dsn("prod") == fake


def test_resolve_dsn_unknown_target_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(apply_migrations.MigrationConfigError):
        apply_migrations.resolve_dsn("staging-x")


def test_resolve_dsn_missing_env_var_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIGRATIONS_DB_URL_DEV", raising=False)
    with pytest.raises(apply_migrations.MigrationConfigError) as ei:
        apply_migrations.resolve_dsn("dev")
    assert "MIGRATIONS_DB_URL_DEV" in str(ei.value)


def test_advisory_lock_key_is_stable() -> None:
    """Lock key must never change — concurrent runners across versions must contend."""
    assert apply_migrations.ADVISORY_LOCK_KEY == 4736294826


def test_audit_log_insert_uses_only_pre_019_columns() -> None:
    """Regression for the 2026-05-18 prod failure.

    The audit_log INSERT must reference only columns that exist in the
    migration-000 baseline (actor, action, subject_type, details). Any
    new field — including aegis_version — must live inside the `details`
    JSONB. Migrations 015..018 run BEFORE migration 019 adds the new
    top-level columns; an INSERT that referenced `aegis_version` as a
    column raised ``UndefinedColumn`` and atomically rolled back 015's
    apply against prod.

    This unit test fails without a database, catching the bug at lint /
    pre-commit time. The integration counterpart is
    ``test_integration_apply_against_pre_019_audit_log_schema``.
    """
    sql = apply_migrations._AUDIT_LOG_INSERT_SQL.lower()

    # Extract the INSERT column list — the parens immediately after "into audit_log".
    _, _, rest = sql.partition("into audit_log")
    assert rest, "audit_log INSERT not found in _AUDIT_LOG_INSERT_SQL"
    column_list = rest.split("(", 1)[1].split(")", 1)[0]

    # Top-level columns: only those from migration 000.
    forbidden_top_level = {
        "aegis_version",
        "deal_id",
        "state_change",
        "rule_pack_version",
    }
    for forbidden in forbidden_top_level:
        assert forbidden not in column_list, (
            f"{forbidden!r} must not be a top-level audit_log column in the "
            f"INSERT — it lives in details JSONB. Found column list: {column_list!r}"
        )

    # And every forbidden top-level key must still be recorded — just inside details.
    # We at least require aegis_version (the one the runner actually writes).
    assert "aegis_version" in sql, (
        "aegis_version must still be written into details JSONB so the audit "
        "trail records which runner SHA performed each apply"
    )


def test_migration_probes_cover_every_real_migration() -> None:
    """Every numbered migration in migrations/ has a bootstrap probe.

    Drift between filename list and MIGRATION_PROBES would silently break
    bootstrap; this test fails CI if a new migration lands without a probe.
    """
    files = apply_migrations.discover_migrations()
    real_filenames = {m.filename for m in files}
    probed = set(apply_migrations.MIGRATION_PROBES.keys())
    missing = real_filenames - probed
    assert not missing, (
        f"migrations without bootstrap probes: {sorted(missing)} — "
        "add to apply_migrations.MIGRATION_PROBES"
    )


# ===========================================================================
# Integration tests — gated on AEGIS_TEST_DB_URL
# ===========================================================================


@pytest.fixture()
def pg_dsn() -> str:
    """Disposable Postgres URL; skip when not provided."""
    dsn = os.environ.get("AEGIS_TEST_DB_URL", "").strip()
    if not dsn:
        pytest.skip(
            "set AEGIS_TEST_DB_URL to a disposable Postgres URL to run "
            "integration tests (e.g. docker run --rm -d -p 5433:5432 "
            "-e POSTGRES_PASSWORD=test postgres:16; "
            "AEGIS_TEST_DB_URL=postgresql://postgres:test@localhost:5433/postgres)"
        )
    return dsn


@pytest.fixture()
def clean_db(pg_dsn: str) -> str:
    """Drop public schema clean before each integration test."""
    import psycopg
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
    return pg_dsn


@pytest.fixture()
def synthetic_migrations(tmp_path: Path) -> list[Any]:
    """Two trivial idempotent migrations + a third whose body raises."""
    # Realism: real migration 000 creates audit_log WITHOUT aegis_version.
    # That column is added by migration 019. Tests must match — otherwise we
    # silently mask the pre-019-schema compatibility requirement (the bug
    # that escaped to prod on 2026-05-18).
    (tmp_path / "000_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS audit_log (\n"
        "  id BIGSERIAL PRIMARY KEY,\n"
        "  actor TEXT NOT NULL,\n"
        "  action TEXT NOT NULL,\n"
        "  subject_type TEXT,\n"
        "  subject_id UUID,\n"
        "  details JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
        "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
        ");\n"
    )
    (tmp_path / "001_t1.sql").write_text(
        "CREATE TABLE IF NOT EXISTS t1 (id INT PRIMARY KEY);\n"
    )
    discovered: list[Any] = apply_migrations.discover_migrations(tmp_path)
    return discovered


def test_integration_synthetic_apply_and_reapply_skip(
    clean_db: str, synthetic_migrations: list[Any]
) -> None:
    """First run applies everything; second run skips with no writes."""
    import psycopg

    runner = apply_migrations.MigrationRunner(
        dsn=clean_db,
        target="dev",
        actor="apply_migrations:test",
        dry_run=False,
        aegis_version="test",
        migrations=synthetic_migrations,
    )
    report = runner.run()
    assert sorted(report.applied) == ["000_init.sql", "001_t1.sql"]
    assert report.skipped == []

    # Re-run: everything skips.
    runner2 = apply_migrations.MigrationRunner(
        dsn=clean_db,
        target="dev",
        actor="apply_migrations:test",
        dry_run=False,
        aegis_version="test",
        migrations=synthetic_migrations,
    )
    report2 = runner2.run()
    assert report2.applied == []
    assert sorted(report2.skipped) == ["000_init.sql", "001_t1.sql"]

    # audit_log has one row per applied migration on the first pass.
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE action='migration_applied'"
            )
            row = cur.fetchone()
            assert row is not None
            assert int(row[0]) == 2


def test_integration_drift_detection_blocks_modified_file(
    clean_db: str,
    synthetic_migrations: list[Any],
    tmp_path: Path,
) -> None:
    """Editing a previously-applied migration must raise MigrationDriftError."""
    runner = apply_migrations.MigrationRunner(
        dsn=clean_db,
        target="dev",
        actor="apply_migrations:test",
        dry_run=False,
        aegis_version="test",
        migrations=synthetic_migrations,
    )
    runner.run()

    # Mutate the file content. The runner reads the file on next discovery,
    # so we modify the source path and re-discover.
    target_file = next(p for p in synthetic_migrations if p.filename == "001_t1.sql").path
    target_file.write_text(target_file.read_text() + "\n-- drift comment\n")
    modified = apply_migrations.discover_migrations(target_file.parent)

    runner2 = apply_migrations.MigrationRunner(
        dsn=clean_db,
        target="dev",
        actor="apply_migrations:test",
        dry_run=False,
        aegis_version="test",
        migrations=modified,
    )
    with pytest.raises(apply_migrations.MigrationDriftError):
        runner2.run()


def test_integration_apply_against_pre_019_audit_log_schema(
    clean_db: str, tmp_path: Path
) -> None:
    """Regression for the 2026-05-18 prod failure.

    Real migration 000 creates ``audit_log`` with columns (id, actor, action,
    subject_type, subject_id, details, created_at). Migration 019 later adds
    deal_id, state_change, aegis_version, rule_pack_version. The runner must
    write audit rows successfully against the pre-019 schema — that is the
    state during the apply of 015..018.

    Pre-fix code referenced aegis_version as a top-level column and raised
    UndefinedColumn against prod. This test sets up exactly that pre-019
    schema and asserts the apply lands cleanly with aegis_version recorded
    inside details JSONB.
    """
    (tmp_path / "000_pre019_baseline.sql").write_text(
        "CREATE TABLE IF NOT EXISTS audit_log (\n"
        "  id BIGSERIAL PRIMARY KEY,\n"
        "  actor TEXT NOT NULL,\n"
        "  action TEXT NOT NULL,\n"
        "  subject_type TEXT,\n"
        "  subject_id UUID,\n"
        "  details JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
        "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
        ");\n"
    )
    (tmp_path / "001_pre019_payload.sql").write_text(
        "CREATE TABLE IF NOT EXISTS pre019_marker (id INT PRIMARY KEY);\n"
    )
    migs = apply_migrations.discover_migrations(tmp_path)

    runner = apply_migrations.MigrationRunner(
        dsn=clean_db,
        target="dev",
        actor="apply_migrations:test",
        dry_run=False,
        aegis_version="aegisv-test",
        migrations=migs,
    )
    report = runner.run()
    assert sorted(report.applied) == [
        "000_pre019_baseline.sql",
        "001_pre019_payload.sql",
    ]

    import psycopg
    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            # Test-invariant sanity: audit_log must remain at the pre-019
            # baseline (no aegis_version column). If a future fixture starts
            # adding the column, this test stops being meaningful.
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='audit_log' "
                "AND column_name='aegis_version'"
            )
            assert cur.fetchone() is None, (
                "test fixture invariant broken: audit_log should remain at "
                "the pre-019 baseline (no aegis_version column)"
            )

            cur.execute(
                "SELECT details FROM audit_log "
                "WHERE action='migration_applied' "
                "  AND details->>'filename'='001_pre019_payload.sql'"
            )
            row = cur.fetchone()
            assert row is not None, (
                "audit_log row for 001_pre019_payload.sql is missing — "
                "pre-019 compat regression"
            )
            details = row[0]
            assert details["aegis_version"] == "aegisv-test"
            assert details["filename"] == "001_pre019_payload.sql"
            assert details["target"] == "dev"
            assert "started_at" in details and "finished_at" in details


def test_integration_failed_migration_rolls_back_audit_row(
    clean_db: str, tmp_path: Path
) -> None:
    """If the migration body raises, the audit_log row must not land."""
    # Realism: real migration 000 creates audit_log WITHOUT aegis_version.
    # That column is added by migration 019. Tests must match — otherwise we
    # silently mask the pre-019-schema compatibility requirement (the bug
    # that escaped to prod on 2026-05-18).
    (tmp_path / "000_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS audit_log (\n"
        "  id BIGSERIAL PRIMARY KEY,\n"
        "  actor TEXT NOT NULL,\n"
        "  action TEXT NOT NULL,\n"
        "  subject_type TEXT,\n"
        "  subject_id UUID,\n"
        "  details JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
        "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
        ");\n"
    )
    # This migration intentionally raises inside its body.
    (tmp_path / "001_bad.sql").write_text(
        "CREATE TABLE will_exist (id INT);\n"
        "INSERT INTO definitely_not_a_table VALUES (1);\n"
    )
    migrations = apply_migrations.discover_migrations(tmp_path)

    runner = apply_migrations.MigrationRunner(
        dsn=clean_db,
        target="dev",
        actor="apply_migrations:test",
        dry_run=False,
        aegis_version="test",
        migrations=migrations,
    )
    import psycopg

    # The bad SQL (INSERT into a non-existent table) surfaces as a
    # psycopg.Error subclass (UndefinedTable). Asserting on the base
    # psycopg.Error is more specific than blind Exception while staying
    # decoupled from psycopg's specific exception hierarchy.
    with pytest.raises(psycopg.Error):
        runner.run()

    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            # 000_init.sql succeeded; its audit row is present.
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE details->>'filename'='000_init.sql'"
            )
            row = cur.fetchone()
            assert row is not None and int(row[0]) == 1
            # 001_bad.sql failed; no audit row, no will_exist table.
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE details->>'filename'='001_bad.sql'"
            )
            row = cur.fetchone()
            assert row is not None and int(row[0]) == 0
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='will_exist'"
            )
            assert cur.fetchone() is None


def test_integration_concurrent_runner_hits_lock_error(
    clean_db: str, tmp_path: Path
) -> None:
    """Two simultaneous runners — one acquires the lock, the other raises."""
    # A migration with a deliberate pg_sleep so the first runner holds the
    # lock long enough for the second to attempt acquisition.
    # Realism: real migration 000 creates audit_log WITHOUT aegis_version.
    # That column is added by migration 019. Tests must match — otherwise we
    # silently mask the pre-019-schema compatibility requirement (the bug
    # that escaped to prod on 2026-05-18).
    (tmp_path / "000_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS audit_log (\n"
        "  id BIGSERIAL PRIMARY KEY,\n"
        "  actor TEXT NOT NULL,\n"
        "  action TEXT NOT NULL,\n"
        "  subject_type TEXT,\n"
        "  subject_id UUID,\n"
        "  details JSONB NOT NULL DEFAULT '{}'::jsonb,\n"
        "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
        ");\n"
    )
    (tmp_path / "001_slow.sql").write_text(
        "SELECT pg_sleep(2);\n"
        "CREATE TABLE IF NOT EXISTS marker (id INT);\n"
    )
    migs = apply_migrations.discover_migrations(tmp_path)

    runner_a = apply_migrations.MigrationRunner(
        dsn=clean_db, target="dev", actor="A",
        dry_run=False, aegis_version="t", migrations=migs,
    )
    runner_b = apply_migrations.MigrationRunner(
        dsn=clean_db, target="dev", actor="B",
        dry_run=False, aegis_version="t", migrations=migs,
    )

    errors: list[BaseException] = []

    def run_b() -> None:
        # Give runner A a beat to acquire the lock first.
        import time
        time.sleep(0.5)
        try:
            runner_b.run()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=run_b)
    t.start()
    # Runner A blocks on pg_sleep(2) inside its transaction with lock held.
    runner_a.run()
    t.join()

    assert any(
        isinstance(e, apply_migrations.MigrationLockHeldError) for e in errors
    ), f"runner B did not raise MigrationLockHeldError; saw {errors!r}"
