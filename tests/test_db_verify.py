"""Tests for scripts/db_verify.py — header parser hardening.

Loaded via importlib because db_verify.py is operator tooling under
scripts/ and isn't on sys.path otherwise (same pattern as
test_apply_migrations.py).

Scope is intentionally narrow: the _parse_headers regression that
landed with migration-032-populated.sql, plus the happy paths the
regression must not regress. DSN resolution, write-keyword refusal,
and the actual psycopg execution path are out of scope here — those
need a live DB and live up via integration / operator smoke.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("db_verify", REPO_ROOT / "scripts" / "db_verify.py")
assert _spec is not None and _spec.loader is not None
db_verify = importlib.util.module_from_spec(_spec)
sys.modules["db_verify"] = db_verify
_spec.loader.exec_module(db_verify)


def test_parse_headers_first_occurrence_wins_on_expect_rows_min() -> None:
    """Regression: a commentary line that quotes EXPECT_ROWS_MIN must not
    re-trigger int() parsing. Matches the exact shape from
    scripts/db_checks/migration-032-populated.sql which crashed with
    ValueError before the fix."""
    sql = (
        "-- DESCRIPTION: chunk-2 populated check\n"
        "-- EXPECT_ROWS_MIN: 1\n"
        "--\n"
        "-- EXPECT_ROWS_MIN: 1 is the load-bearing assertion — zero rows means\n"
        "-- either (a) no docs parsed since chunk 2, or (b) writer broken.\n"
        "SELECT 1;\n"
    )
    description, expect_rows, expect_rows_min = db_verify._parse_headers(sql)
    assert description == "chunk-2 populated check"
    assert expect_rows is None
    assert expect_rows_min == 1


def test_parse_headers_first_occurrence_wins_on_expect_rows() -> None:
    """Same regression shape, for EXPECT_ROWS instead of EXPECT_ROWS_MIN."""
    sql = (
        "-- EXPECT_ROWS: 2\n"
        "-- EXPECT_ROWS: 2 here means exactly two trigger rows, not three\n"
        "SELECT 1;\n"
    )
    _, expect_rows, _ = db_verify._parse_headers(sql)
    assert expect_rows == 2


def test_parse_headers_first_occurrence_wins_on_description() -> None:
    """Description should bind to the first DESCRIPTION: line — a later
    commentary line that says "DESCRIPTION: …" must not overwrite it."""
    sql = (
        "-- DESCRIPTION: the real description\n"
        "-- DESCRIPTION: don't pick me; I'm prose\n"
        "SELECT 1;\n"
    )
    description, _, _ = db_verify._parse_headers(sql)
    assert description == "the real description"


def test_parse_headers_happy_path_all_three_headers() -> None:
    sql = (
        "-- DESCRIPTION: triggers must be immutable\n"
        "-- EXPECT_ROWS: 3\n"
        "-- EXPECT_ROWS_MIN: 1\n"
        "SELECT trg.tgname FROM pg_trigger trg;\n"
    )
    description, expect_rows, expect_rows_min = db_verify._parse_headers(sql)
    assert description == "triggers must be immutable"
    assert expect_rows == 3
    assert expect_rows_min == 1


def test_parse_headers_stops_at_first_non_comment_line() -> None:
    """Headers below the first non-`--` line are not parsed — verifies the
    early break preserves intent (an EXPECT_ROWS: inside a CTE comment
    block, for example, would not silently retune the assertion)."""
    sql = "-- DESCRIPTION: real desc\n-- EXPECT_ROWS_MIN: 1\nSELECT 1;\n-- EXPECT_ROWS_MIN: 99\n"
    _, _, expect_rows_min = db_verify._parse_headers(sql)
    assert expect_rows_min == 1


def test_parse_headers_no_headers_returns_defaults() -> None:
    sql = "SELECT 1;\n"
    description, expect_rows, expect_rows_min = db_verify._parse_headers(sql)
    assert description == ""
    assert expect_rows is None
    assert expect_rows_min is None


def test_parse_headers_malformed_int_still_raises() -> None:
    """A genuinely-typo'd header (no commentary repeat) must surface
    loudly, not be silently swallowed. The first-occurrence-wins guard
    must not double as a malformed-value swallower — operator intent in
    a header line is load-bearing."""
    import pytest

    sql = "-- EXPECT_ROWS: not-a-number\nSELECT 1;\n"
    with pytest.raises(ValueError):
        db_verify._parse_headers(sql)
