"""Migration shape tests.

Without a live Postgres connection these tests verify the SQL files
exist with the right column definitions and constraints. A future
integration suite can apply the migrations against a test Postgres
container; until then, file-shape assertions catch typos / drift.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _read(name: str) -> str:
    path = MIGRATIONS_DIR / name
    assert path.is_file(), f"missing migration: {name}"
    return path.read_text(encoding="utf-8")


# --- 004 disclosure_transmission_log ----------------------------------------


def test_004_creates_disclosure_transmission_log_table() -> None:
    sql = _read("004_disclosure_transmission_log.sql")
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+disclosure_transmission_log",
        sql,
        re.IGNORECASE,
    )


def test_004_has_required_columns_per_dossier() -> None:
    sql = _read("004_disclosure_transmission_log.sql")
    # The dossier-listed fields must all be present as column declarations.
    for column in (
        "id",
        "deal_id",
        "funder_id",
        "disclosure_doc_hash",
        "transmitted_at",
        "transmitted_to_email",
        "merchant_acknowledged_at",
        "funder_notified_at",
        "retention_until",
    ):
        assert re.search(rf"^\s*{column}\b", sql, re.IGNORECASE | re.MULTILINE), (
            f"column {column!r} missing in 004 migration"
        )


def test_004_retention_until_is_4y_plus_30d_buffer() -> None:
    """Per dossier 10: retention_until = transmitted_at + 4 years + 30 day buffer."""
    sql = _read("004_disclosure_transmission_log.sql")
    # The expression locks both the source column and the interval shape.
    pattern = (
        r"GENERATED ALWAYS AS\s*\(\s*transmitted_at\s*\+\s*"
        r"INTERVAL\s*'4 years 30 days'\s*\)\s*STORED"
    )
    assert re.search(pattern, sql, re.IGNORECASE)


def test_004_has_indexes_on_deal_funder_retention() -> None:
    sql = _read("004_disclosure_transmission_log.sql")
    for col in ("deal_id", "funder_id", "retention_until"):
        assert re.search(
            rf"CREATE INDEX[^;]*ON\s+disclosure_transmission_log\s*\([^)]*{col}",
            sql,
            re.IGNORECASE,
        ), f"missing index covering {col!r} in 004 migration"


# --- 005 funders.requires_coj -----------------------------------------------


def test_005_adds_requires_coj_column() -> None:
    sql = _read("005_funders_requires_coj.sql")
    assert re.search(
        r"ALTER TABLE\s+funders\s+ADD COLUMN IF NOT EXISTS\s+requires_coj\s+BOOLEAN",
        sql,
        re.IGNORECASE,
    )


def test_005_default_is_false() -> None:
    """Existing funders are assumed not to require CoJ until operator updates."""
    sql = _read("005_funders_requires_coj.sql")
    assert re.search(r"requires_coj\s+BOOLEAN[^;]*DEFAULT\s+false", sql, re.IGNORECASE)


# --- 006 funders.aegis_compensation_disclosure_text -------------------------


def test_006_adds_aegis_compensation_disclosure_text_column() -> None:
    sql = _read("006_funders_aegis_compensation_disclosure.sql")
    assert re.search(
        r"ALTER TABLE\s+funders\s+ADD COLUMN IF NOT EXISTS\s+"
        r"aegis_compensation_disclosure_text\s+TEXT",
        sql,
        re.IGNORECASE,
    )


def test_006_default_is_empty_string_not_null() -> None:
    """Empty default + NOT NULL: missing text is the disclosure-guard signal."""
    sql = _read("006_funders_aegis_compensation_disclosure.sql")
    assert re.search(
        r"aegis_compensation_disclosure_text\s+TEXT\s+NOT NULL\s+DEFAULT\s+''",
        sql,
        re.IGNORECASE,
    )


def test_006_cites_section_600_21_in_comments() -> None:
    """Migration self-documents the regulatory source for future reviewers."""
    sql = _read("006_funders_aegis_compensation_disclosure.sql")
    assert "600.21(f)" in sql


# --- 007 funders.charges_merchant_advance_fees ------------------------------


def test_007_adds_charges_merchant_advance_fees_column() -> None:
    sql = _read("007_funders_charges_merchant_advance_fees.sql")
    assert re.search(
        r"ALTER TABLE\s+funders\s+ADD COLUMN IF NOT EXISTS\s+"
        r"charges_merchant_advance_fees\s+BOOLEAN",
        sql,
        re.IGNORECASE,
    )


def test_007_default_is_false_not_null() -> None:
    """Existing funders are assumed not to charge merchant advance fees."""
    sql = _read("007_funders_charges_merchant_advance_fees.sql")
    assert re.search(
        r"charges_merchant_advance_fees\s+BOOLEAN\s+NOT NULL\s+DEFAULT\s+false",
        sql,
        re.IGNORECASE,
    )


def test_007_cites_section_559_9614_in_comments() -> None:
    """Migration self-documents the regulatory source (FL FCFDL § 559.9614(1)(a))."""
    sql = _read("007_funders_charges_merchant_advance_fees.sql")
    assert "559.9614" in sql
