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


# --- 036 disclosure_transmissions (R0.5) -----------------------------------


def test_036_creates_disclosure_transmissions_table() -> None:
    sql = _read("036_disclosure_transmissions.sql")
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+disclosure_transmissions",
        sql,
        re.IGNORECASE,
    )


def test_036_has_required_columns_for_ca_ny_audit_trail() -> None:
    """Every regulator-facing field a CA § 952 / NY § 600.21 audit needs."""
    sql = _read("036_disclosure_transmissions.sql")
    for column in (
        "id",
        "deal_id",
        "merchant_id",
        "state",
        "disclosure_version",
        "template_path",
        "html_sha256",
        "recipient_email",
        "sent_at",
        "sent_by",
        "apr",
        "funding_provided",
        "finance_charge",
        "estimated_total_payment",
        "estimated_term_days",
        "factor_rate",
        "holdback_pct",
        "metadata",
        "retention_until",
    ):
        assert re.search(rf"^\s*{column}\b", sql, re.IGNORECASE | re.MULTILINE), (
            f"column {column!r} missing in 036 migration"
        )


def test_036_retention_until_is_4y_plus_30d_buffer() -> None:
    """4-year CA § 952 + NY § 600 floor + 30-day buffer.

    DEFAULT expression (not GENERATED STORED) because current Postgres
    marks every TIMESTAMPTZ + INTERVAL as STABLE — STORED generated
    columns reject it. DEFAULTs don't require immutability; NOW() +
    INTERVAL '1490 days' evaluates once per row at insert. See migration
    header for the longer explanation.
    """
    sql = _read("036_disclosure_transmissions.sql")
    pattern = (
        r"retention_until\s+TIMESTAMPTZ\s+NOT NULL\s+DEFAULT\s*"
        r"\(\s*NOW\(\)\s*\+\s*INTERVAL\s*'1490 days'\s*\)"
    )
    assert re.search(pattern, sql, re.IGNORECASE)


def test_036_has_indexes_for_audit_queries() -> None:
    """Indexes on (deal_id), (merchant_id), (state, sent_at), (retention_until)
    cover the four regulator-shaped queries."""
    sql = _read("036_disclosure_transmissions.sql")
    for index_pattern in (
        r"CREATE INDEX[^;]*ON\s+disclosure_transmissions\s*\(\s*deal_id",
        r"CREATE INDEX[^;]*ON\s+disclosure_transmissions\s*\(\s*merchant_id",
        r"CREATE INDEX[^;]*ON\s+disclosure_transmissions\s*\(\s*state\s*,\s*sent_at",
        r"CREATE INDEX[^;]*ON\s+disclosure_transmissions\s*\(\s*retention_until",
    ):
        assert re.search(index_pattern, sql, re.IGNORECASE), (
            f"missing index pattern in 036: {index_pattern}"
        )


def test_036_enables_row_level_security() -> None:
    """Compliance tables ship with RLS enabled (mirrors migration 016)."""
    sql = _read("036_disclosure_transmissions.sql")
    assert re.search(
        r"ALTER TABLE\s+disclosure_transmissions\s+ENABLE ROW LEVEL SECURITY",
        sql,
        re.IGNORECASE,
    )


def test_036_cites_ca_952_and_ny_600_in_comments() -> None:
    """Migration self-documents both source statutes."""
    sql = _read("036_disclosure_transmissions.sql")
    assert "952" in sql
    assert "600" in sql


# --- 037 scoring_shadow_disagreements (R1.6) --------------------------------


def test_037_creates_scoring_shadow_disagreements_table() -> None:
    sql = _read("037_scoring_shadow_disagreements.sql")
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+scoring_shadow_disagreements",
        sql,
        re.IGNORECASE,
    )


def test_037_has_required_columns_per_audit_spec() -> None:
    """R1.6 audit spec: every column the triage queue needs."""
    sql = _read("037_scoring_shadow_disagreements.sql")
    for column in (
        "id",
        "merchant_id",
        "deal_id",
        "comparison_run_at",
        "legacy_fraud_score",
        "legacy_tier",
        "legacy_recommendation",
        "legacy_hard_declines",
        "track_a_verdict",
        "track_b_band",
        "track_c_panel",
        "category",
        "evidence",
        "triaged_by",
        "triaged_at",
        "triage_decision",
        "triage_notes",
    ):
        assert re.search(rf"^\s*{column}\b", sql, re.IGNORECASE | re.MULTILINE), (
            f"column {column!r} missing in 037 migration"
        )


def test_037_merchant_id_is_not_null() -> None:
    """merchant_id is the primary triage axis; cannot be null."""
    sql = _read("037_scoring_shadow_disagreements.sql")
    assert re.search(r"merchant_id\s+UUID\s+NOT NULL", sql, re.IGNORECASE)


def test_037_category_is_not_null() -> None:
    """Every row must carry one of the five CAT_* values."""
    sql = _read("037_scoring_shadow_disagreements.sql")
    assert re.search(r"category\s+VARCHAR\(48\)\s+NOT NULL", sql, re.IGNORECASE)


def test_037_has_indexes_for_audit_queries() -> None:
    """Indexes on (category, comparison_run_at DESC), (triaged_at),
    (merchant_id, comparison_run_at DESC) — the three regulator-shaped
    triage-queue queries."""
    sql = _read("037_scoring_shadow_disagreements.sql")
    for index_pattern in (
        # (category, comparison_run_at DESC) — regression sentinel queue
        r"CREATE INDEX[^;]*ON\s+scoring_shadow_disagreements\s*"
        r"\(\s*category\s*,\s*comparison_run_at\s+DESC",
        # (triaged_at) — open vs closed scan for the open-view
        r"CREATE INDEX[^;]*ON\s+scoring_shadow_disagreements\s*\(\s*triaged_at",
        # (merchant_id, comparison_run_at DESC) — per-merchant history
        r"CREATE INDEX[^;]*ON\s+scoring_shadow_disagreements\s*"
        r"\(\s*merchant_id\s*,\s*comparison_run_at\s+DESC",
    ):
        assert re.search(index_pattern, sql, re.IGNORECASE), (
            f"missing index pattern in 037: {index_pattern}"
        )


def test_037_enables_row_level_security() -> None:
    """Default-deny RLS: this is internal-only audit data."""
    sql = _read("037_scoring_shadow_disagreements.sql")
    assert re.search(
        r"ALTER TABLE\s+scoring_shadow_disagreements\s+ENABLE ROW LEVEL SECURITY",
        sql,
        re.IGNORECASE,
    )


def test_037_uses_gen_random_uuid_for_primary_key() -> None:
    """Mirrors pgcrypto convention from migrations 016 / 036."""
    sql = _read("037_scoring_shadow_disagreements.sql")
    assert re.search(
        r"id\s+UUID\s+PRIMARY KEY\s+DEFAULT\s+gen_random_uuid\(\)",
        sql,
        re.IGNORECASE,
    )
    assert "pgcrypto" in sql.lower()


# --- 038 scoring_disagreements_open view (R1.6) -----------------------------


def test_038_creates_scoring_disagreements_open_view() -> None:
    sql = _read("038_scoring_disagreements_open_view.sql")
    assert re.search(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+scoring_disagreements_open",
        sql,
        re.IGNORECASE,
    )


def test_038_filters_to_untriaged_rows_only() -> None:
    """The view's purpose: every row where triaged_at IS NULL."""
    sql = _read("038_scoring_disagreements_open_view.sql")
    assert re.search(r"triaged_at\s+IS\s+NULL", sql, re.IGNORECASE)


def test_038_orders_regression_sentinel_first() -> None:
    """``old-caught-something-new-misses`` MUST surface first in the queue."""
    sql = _read("038_scoring_disagreements_open_view.sql")
    # Find the CASE ordering and assert old-caught is rank 0.
    case_block = re.search(
        r"CASE\s+d\.category(.*?)END",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert case_block is not None, "missing CASE ordering on category"
    case_text = case_block.group(1)
    # 'old-caught-something-new-misses' must appear AHEAD of all other
    # categories in the WHEN list.
    positions = {}
    for cat in (
        "old-caught-something-new-misses",
        "new-is-better",
        "genuinely-ambiguous",
        "agreement",
        "insufficient-new-data",
    ):
        m = re.search(rf"'{re.escape(cat)}'", case_text)
        assert m is not None, f"category {cat!r} missing from view ORDER BY"
        positions[cat] = m.start()
    # Regression sentinel must be the first WHEN clause.
    assert positions["old-caught-something-new-misses"] == min(positions.values())


def test_038_orders_by_comparison_run_at_desc_within_category() -> None:
    """Within each category bucket, newest evidence surfaces first."""
    sql = _read("038_scoring_disagreements_open_view.sql")
    assert re.search(
        r"comparison_run_at\s+DESC",
        sql,
        re.IGNORECASE,
    )


def test_038_selects_from_scoring_shadow_disagreements() -> None:
    """The view must read from the migration-037 table."""
    sql = _read("038_scoring_disagreements_open_view.sql")
    assert re.search(
        r"FROM\s+scoring_shadow_disagreements",
        sql,
        re.IGNORECASE,
    )


# --- 070 decisions immutability + backfill ----------------------------------


def test_070_reasserts_block_decision_modification_function() -> None:
    """Migration 070 idempotently re-installs the immutability trigger
    function from migration 015 so a fresh project that lost the
    triggers (manual ops / restored backup) gets them back without
    depending on 015 having run first."""
    sql = _read("070_decisions_immutable.sql")
    assert re.search(
        r"CREATE OR REPLACE FUNCTION\s+block_decision_modification",
        sql,
        re.IGNORECASE,
    )
    # The error message must match the 015 contract so existing
    # callers / tests that check for the substring keep working.
    assert "decisions table is append-only" in sql


def test_070_installs_both_update_and_delete_triggers() -> None:
    sql = _read("070_decisions_immutable.sql")
    for trig in ("decisions_no_update", "decisions_no_delete"):
        assert re.search(
            rf"CREATE TRIGGER\s+{trig}\s+BEFORE\s+(UPDATE|DELETE)\s+ON\s+decisions",
            sql,
            re.IGNORECASE,
        ), f"trigger {trig} missing"


def test_070_extends_backfill_unique_index_to_2026_06_cohort() -> None:
    """The partial unique index in migration 015 only covered the
    ``backfill_2026_05`` cohort. Migration 070 extends the WHERE clause
    to also cover ``backfill_2026_06`` so a re-run of the 070 backfill
    after a partial failure stays idempotent."""
    sql = _read("070_decisions_immutable.sql")
    # DROP-and-CREATE to widen the WHERE clause.
    assert re.search(
        r"DROP INDEX IF EXISTS\s+uq_decisions_backfill_per_deal",
        sql,
        re.IGNORECASE,
    )
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS\s+uq_decisions_backfill_per_deal",
        sql,
        re.IGNORECASE,
    )
    assert "backfill_2026_05" in sql
    assert "backfill_2026_06" in sql


def test_070_backfill_inserts_against_real_schema_columns() -> None:
    """The backfill SELECT must source columns that actually exist on
    the analyses + documents tables — see 000_foundation.sql and
    002_analyses_source_ids.sql + 033_documents_storage_and_retention.sql."""
    sql = _read("070_decisions_immutable.sql")
    # Real analyses _source_ids columns (per migration 002).
    for col in (
        "avg_daily_balance_source_ids",
        "true_revenue_source_ids",
        "num_nsf_source_ids",
        "days_negative_source_ids",
        "mca_daily_total_source_ids",
    ):
        assert col in sql, f"backfill should reference {col}"
    # documents-side columns.
    assert "fraud_score" in sql
    assert "fraud_score_breakdown" in sql
    assert "sha256_original" in sql
    # And it must skip when a decisions row already exists for the doc.
    assert re.search(
        r"NOT EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+decisions",
        sql,
        re.IGNORECASE,
    )


def test_070_backfill_stamps_2026_06_cohort_label() -> None:
    sql = _read("070_decisions_immutable.sql")
    assert "'backfill_2026_06'" in sql


def test_070_writes_audit_row_for_backfill_event() -> None:
    """Phase 2 acceptance: every state change writes to audit_log. The
    backfill itself is a state change; migration 070 must record it."""
    sql = _read("070_decisions_immutable.sql")
    assert re.search(
        r"INSERT INTO audit_log",
        sql,
        re.IGNORECASE,
    )
    assert "decisions.backfilled" in sql
    assert "migration_070" in sql
