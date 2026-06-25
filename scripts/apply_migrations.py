"""3C-extra migration runner — replaces the manual `_all_in_order.sql` paste.

Walks `migrations/<NNN>_*.sql` in lexicographic order, applies whatever
isn't yet recorded in `schema_migrations`, wraps every apply (migration
body + schema_migrations row + audit_log row) in a single transaction,
and serializes against concurrent runners via a Postgres advisory lock.

Spec source: `~/.claude/projects/.../memory/project-3c-extra-migration-runner-spec.md`.

Key invariants
--------------
* Each migration is one transaction. Failure rolls back the schema change
  AND the schema_migrations row AND the audit_log row, together.
* Re-apply protection: a (filename, sha256) match in schema_migrations
  skips the file. A filename match with a different sha256 raises
  ``MigrationDriftError`` — applied SQL is never silently re-executed.
* Concurrency: ``pg_try_advisory_lock(4736294826)`` is acquired before
  the bootstrap probe and held until final commit / rollback. A second
  runner gets ``MigrationLockHeldError`` with the holder's pid.
* Prod guard: a resolved DSN containing the prod project ref
  (``tprpbomqcucuxnszeafo``) requires ``--target prod`` explicitly.
* Bootstrap: on first run against a DB without ``schema_migrations``,
  each migration's effect is probed via ``MIGRATION_PROBES``; migrations
  whose probe returns a row are backfilled into ``schema_migrations``
  with ``applied_by='manual_pre_runner'`` so the runner does not try to
  re-apply already-deployed schema.

Usage
-----
    uv run python scripts/apply_migrations.py --target prod --dry-run
    uv run python scripts/apply_migrations.py --target prod
    make migrate TARGET=prod DRY_RUN=1
    make migrate TARGET=prod

Read about the audit-log row retrieval query in deploy/RUNBOOK.md under
"Database migrations".
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

ADVISORY_LOCK_KEY = 4736294826
PROD_PROJECT_REF = "tprpbomqcucuxnszeafo"

# audit_log INSERT.
#
# Schema compatibility: this statement must work against BOTH
#   - the original audit_log (migration 000: actor, action, subject_type,
#     subject_id, details, created_at), and
#   - the extended audit_log (migration 019 adds deal_id, state_change,
#     aegis_version, rule_pack_version).
#
# Reason: migrations 015..018 land BEFORE 019 in apply order, so when the
# runner writes the audit row for those, aegis_version does NOT exist as a
# column. The 2026-05-18 prod attempt failed exactly here with:
#   UndefinedColumn: column "aegis_version" of relation "audit_log" does not exist
#
# Fix: every field beyond the migration-000 minimum lives INSIDE the
# `details` JSONB. Future audit_log extensions remain forward-compatible
# without changes to this runner.
_AUDIT_LOG_INSERT_SQL = """
INSERT INTO audit_log (actor, action, subject_type, details)
VALUES (%s, 'migration_applied', 'migration',
        jsonb_build_object(
          'filename', %s::text,
          'sha256', %s::text,
          'target', %s::text,
          'started_at', %s::text,
          'finished_at', %s::text,
          'aegis_version', %s::text
        ))
"""

_DSN_ENV_BY_TARGET = {
    "dev": "MIGRATIONS_DB_URL_DEV",
    "staging": "MIGRATIONS_DB_URL_STAGING",
    "prod": "MIGRATIONS_DB_URL_PROD",
}

_MIGRATION_FILENAME_RE = re.compile(r"^\d{3}_.+\.sql$")

# Bootstrap probes: one SELECT per migration whose presence implies the
# migration body has already executed. The runner only fires bootstrap
# when schema_migrations is empty; the probes are best-effort but
# accurate against the existing migrations 000..021.
MIGRATION_PROBES: dict[str, str] = {
    "000_foundation.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='merchants'"
    ),
    "001_pgcrypto_and_transactions.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='transactions'"
    ),
    "002_analyses_source_ids.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='analyses' "
        "AND column_name='avg_daily_balance_source_ids'"
    ),
    "003_funders_table.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='funders'"
    ),
    "004_disclosure_transmission_log.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='disclosure_transmission_log'"
    ),
    "005_funders_requires_coj.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='requires_coj'"
    ),
    "006_funders_aegis_compensation_disclosure.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='aegis_compensation_disclosure_text'"
    ),
    "007_funders_charges_merchant_advance_fees.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='charges_merchant_advance_fees'"
    ),
    "008_merchants_intake_fields.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='entity_type'"
    ),
    "009_analyses_monthly_breakdown.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='analyses' "
        "AND column_name='monthly_breakdown'"
    ),
    "010_add_zoho_lead_id.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='zoho_lead_id'"
    ),
    "011_enable_rls.sql": (
        "SELECT 1 FROM pg_tables "
        "WHERE schemaname='public' AND tablename='merchants' "
        "AND rowsecurity=true"
    ),
    "012_deals_view.sql": (
        "SELECT 1 FROM information_schema.views WHERE table_schema='public' AND table_name='deals'"
    ),
    "013_submissions_table.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='submissions'"
    ),
    "014_analyses_bank_identity.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='analyses' "
        "AND column_name='bank_name'"
    ),
    "015_decisions.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='decisions'"
    ),
    "016_disclosures.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='disclosures'"
    ),
    "017_overrides.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='overrides'"
    ),
    "018_compliance_obligations.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='compliance_obligations'"
    ),
    "019_audit_log_extend.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='audit_log' "
        "AND column_name='deal_id'"
    ),
    "020_processor_statements.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='processor_statements'"
    ),
    "021_funder_replies.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='funder_replies'"
    ),
    "022_operators_and_roles.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='operators'"
    ),
    "024_audit_log_archive.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='audit_log_archive'"
    ),
    "025_audit_retention_policy.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='audit_retention_policy'"
    ),
    "026_close_lead_id.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='close_lead_id'"
    ),
    "027_funders_contact_and_tiers.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='submission_email'"
    ),
    "028_funders_notes_residual.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='notes_residual'"
    ),
    "029_funders_operator_notes.sql": (
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='operator_notes'"
    ),
    "030_security_advisor_fixes.sql": (
        # operators is the most reliable signal: 011 missed it, and 030 is
        # the migration that turns RLS on there.
        "SELECT 1 FROM pg_tables "
        "WHERE schemaname='public' AND tablename='operators' "
        "AND rowsecurity=true"
    ),
    "031_seed_operators.sql": (
        # filip@commerafunding.com is the canonical seeded admin row.
        # Its presence implies the INSERT body of 031 ran.
        "SELECT 1 FROM operators WHERE email='filip@commerafunding.com'"
    ),
    "032_analyses_pattern_analysis.sql": (
        # pattern_analysis is the column added by 032. Probing on the
        # column itself rather than the GIN index because Supabase's
        # information_schema view doesn't always surface indexes
        # consistently across role contexts; column presence is the
        # load-bearing signal that the migration body ran.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='analyses' "
        "AND column_name='pattern_analysis'"
    ),
    "033_documents_storage_and_retention.sql": (
        # storage_path is the canonical signal for 033: it's the column
        # the worker writes when chunk B ships and is the predicate of
        # idx_documents_retention_until. If storage_path exists, the
        # other three documents columns + merchants.deleted_at landed
        # together (single migration body, single BEGIN/COMMIT).
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='documents' "
        "AND column_name='storage_path'"
    ),
    "034_merchants_provisional.sql": (
        # status is the column added by 034. Probing the column rather
        # than the partial index for the same reason 032 does: index
        # surfacing through Supabase's information_schema view varies
        # by role context, but column presence is the stable signal.
        # If status exists, the three NULL-relaxations and the
        # finalized-has-business-name CHECK landed in the same
        # BEGIN/COMMIT.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='status'"
    ),
    "035_seed_funders_production.sql": (
        # 035 is an INSERT-only seed, so probe one of the canonical
        # seeded funder rows. 'OnDeck' is the tier-1 anchor row;
        # presence implies the migration body executed. Other seed
        # rows (Rapid Finance, Forward Financing, Credibly, Kapitus,
        # Mulligan Funding, CFG Merchant Solutions, Pearl Capital)
        # land in the same INSERT ... ON CONFLICT (name) DO NOTHING
        # block — one row implies all rows landed.
        "SELECT 1 FROM funders WHERE name='OnDeck'"
    ),
    "036_disclosure_transmissions.sql": (
        # Probe the table itself: 036 is a new top-level CREATE TABLE,
        # so table presence is the canonical signal. If the table exists,
        # every column + index + RLS toggle landed together (single
        # CREATE TABLE body + sibling CREATE INDEX statements run in
        # the same migration script). Mirrors the 004 probe pattern
        # for ``disclosure_transmission_log``.
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' "
        "AND table_name='disclosure_transmissions'"
    ),
    "037_scoring_shadow_disagreements.sql": (
        # Probe the table itself: 037 is a new top-level CREATE TABLE
        # (R1.6 Step 2 cutover-prep triage queue). If the table exists,
        # every column + index + RLS toggle landed together in the same
        # migration body. Mirrors the 036 probe pattern.
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' "
        "AND table_name='scoring_shadow_disagreements'"
    ),
    "038_scoring_disagreements_open_view.sql": (
        # Probe the view itself: 038 is a single CREATE OR REPLACE VIEW
        # over the 037 table. View presence is the canonical signal.
        "SELECT 1 FROM information_schema.views "
        "WHERE table_schema='public' "
        "AND table_name='scoring_disagreements_open'"
    ),
    "039_merchants_maturity_date.sql": (
        # Probe the column directly: 039 is a single ADD COLUMN IF NOT
        # EXISTS on merchants. Mirrors the 008 / 026 / 034 probe pattern
        # for additive merchant columns.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='maturity_date'"
    ),
    "040_funder_renewal_attestations.sql": (
        # Probe the table itself: 040 is a new top-level CREATE TABLE
        # (U6 — operator-side renewal-disclosure attestation capture).
        # If the table exists, every column + index + RLS toggle landed
        # together in the same migration body. Mirrors the 036 / 037
        # probe pattern.
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' "
        "AND table_name='funder_renewal_attestations'"
    ),
    "041_analyses_account_holder.sql": (
        # Probe the column directly: 041 is a single ADD COLUMN IF NOT
        # EXISTS on analyses (U15 — durable account_holder for the U12
        # related-account detector). Mirrors the 014 probe pattern for
        # the bank_name / account_last4 columns this column completes
        # the identity triple of.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='analyses' "
        "AND column_name='account_holder'"
    ),
    "042_disclosure_render_events.sql": (
        # Probe the table itself: 042 is a new top-level CREATE TABLE
        # (U16 — persist the disclosure_status state U3 deferred). If
        # the table exists, every column + index + RLS toggle landed
        # together in the same migration body. Mirrors the 036 / 037 /
        # 040 probe pattern.
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' "
        "AND table_name='disclosure_render_events'"
    ),
    "044_merchants_shadow_signals.sql": (
        # Probe the table itself: 044 is a new top-level CREATE TABLE
        # (U22 — persist the cross-statement Pattern list U15 deferred).
        # If the table exists, every column + index + RLS toggle landed
        # together in the same migration body. Mirrors the 036 / 037 /
        # 040 / 042 probe pattern.
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' "
        "AND table_name='merchants_shadow_signals'"
    ),
    "045_remove_seed_funders.sql": (
        # 045 is a DELETE-only cleanup of the 035 placeholder rows. The
        # canonical "already applied" signal is the ABSENCE of any
        # 'Seed row%' funder. Bootstrap will only probe if
        # schema_migrations is empty; in that case a fresh box also has
        # no 035 rows, so the probe trivially passes. On an existing box
        # the runner reads schema_migrations directly and never consults
        # the probe — this entry exists for probe-coverage discipline
        # (test_migration_probes_cover_every_real_migration).
        "SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM funders WHERE notes_residual LIKE 'Seed row%%')"
    ),
    "046_seed_funders_from_manual.sql": (
        # 046 inserts 6 direct funders (Logic Advance, VCG, SwiftSource,
        # Shor, UCS, Highland Hill) parsed from the operator-curated
        # MCA Funder Manual. 'Logic Advance' is the §2 anchor row — its
        # presence implies the migration's INSERT block executed.
        "SELECT 1 FROM funders WHERE name='Logic Advance'"
    ),
    "047_seed_brokers_from_manual.sql": (
        # 047 inserts the 3 broker/affiliate/marketplace funders (§8
        # Splash Advance, §9 Big Think Capital, §10 Bizi Connect). The
        # SQL contains all 9 funders for idempotency, but ON CONFLICT
        # makes the 6 direct rows no-op when 046 has already landed.
        # 'Splash Advance' is the §8 anchor row.
        "SELECT 1 FROM funders WHERE name='Splash Advance'"
    ),
    "048_funders_dedupe_defensive.sql": (
        # 048 is a defensive dedupe — the UNIQUE constraint on
        # funders.name prevents duplicates at the DB layer, so this
        # migration's DELETE is a no-op on a clean table. Probe is
        # trivially-true: the migration body itself never adds or
        # changes schema; once recorded in schema_migrations it's
        # known-applied. (DELETE migrations have no canonical schema
        # artifact to probe.)
        "SELECT 1"
    ),
    "049_logic_ucs_tiers.sql": (
        # 049 writes the tiers JSONB on Logic Advance (4 tiers: Elite/
        # Premium/Standard/High-Risk) and United Capital Source (7
        # products: MCA / Term / LOC / Equipment / Factoring / SBA /
        # Home Equity LOC). Probe is the presence of at least one tier
        # on Logic Advance's tiers JSONB.
        "SELECT 1 FROM funders WHERE name='Logic Advance' AND jsonb_array_length(tiers) > 0"
    ),
    "050_merge_funder_duplicates.sql": (
        # 050 deletes 'Logic Advance Group' and 'Swiftsource Funding'
        # (near-duplicates of canonical 'Logic Advance' and
        # 'SwiftSource Funding'). Probe is the ABSENCE of either
        # variant. Bootstrap on a fresh box trivially passes (variants
        # never existed there); on an existing box the migration's
        # schema_migrations row is the authoritative signal.
        "SELECT 1 WHERE NOT EXISTS "
        "(SELECT 1 FROM funders WHERE name IN "
        "('Logic Advance Group', 'Swiftsource Funding'))"
    ),
    "051_highland_hill_tiers.sql": (
        # 051 writes the 5-rate ladder onto Highland Hill Capital's
        # tiers JSONB (manual §7). Probe is the presence of at least one
        # tier; mirrors the 049 probe pattern.
        "SELECT 1 FROM funders WHERE name='Highland Hill Capital' AND jsonb_array_length(tiers) > 0"
    ),
    "052_shor_notes_enrichment.sql": (
        # 052 enriches Shor Capital's notes_residual with operator-
        # curated §5 context. Probe is any non-empty notes_residual on
        # the row — migration 046 left this field as ''.
        "SELECT 1 FROM funders WHERE name='Shor Capital' AND length(notes_residual) > 0"
    ),
    "053_funder_notes_enrichment.sql": (
        # 053 enriches notes_residual for §3 Velocity Capital Group,
        # §9 Big Think Capital, §10 Bizi Connect with operator-curated
        # manual content. Probe is VCG's note exceeding 100 chars —
        # migration 046 left VCG's notes_residual as '' (length 0), so
        # any value >100 chars is the canonical signal that 053's UPDATE
        # block executed. Big Think + Bizi Connect aren't probed
        # separately: all three UPDATEs share the same migration body /
        # transaction, so one applied row implies all three applied.
        "SELECT 1 FROM funders WHERE name='Velocity Capital Group' AND length(notes_residual) > 100"
    ),
    "054_merchants_close_opportunity_id.sql": (
        # 054 adds nullable ``close_opportunity_id TEXT`` + partial
        # UNIQUE index on it. Probe is the partial-index relation in
        # ``pg_indexes`` — migration 026 used the same shape for
        # ``idx_merchants_close_lead_id``, so the index existence is
        # the canonical signal that this migration's CREATE INDEX
        # executed (the ADD COLUMN alone could have run partway).
        "SELECT 1 FROM pg_indexes "
        "WHERE schemaname='public' AND tablename='merchants' "
        "AND indexname='idx_merchants_close_opportunity_id'"
    ),
    "055_merchants_industry_choice.sql": (
        # 055 adds nullable ``industry_choice TEXT`` to merchants. No
        # index — column isn't a lookup key, only ever read on
        # id-keyed selects. Probe is the column's presence in
        # information_schema.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='industry_choice'"
    ),
    "056_funders_deal_types_velocity_preferred_states.sql": (
        # 056 adds three funder columns. Probe is the
        # deal_types_accepted column — the migration's first ADD COLUMN
        # — so a partial re-application that landed only this column
        # still registers as applied.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='deal_types_accepted'"
    ),
    "057_funder_note_submissions.sql": (
        # 057 creates the funder_note_submissions table. Existence of
        # the table is the load-bearing artifact; the trigger +
        # indexes either land in the same transaction or fail it.
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='funder_note_submissions'"
    ),
    "058_merchants_notes.sql": (
        # 058 is a single ADD COLUMN on merchants. Column existence is
        # the entire effect.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='notes'"
    ),
    "059_bank_layouts.sql": (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='bank_layouts'"
    ),
    "060_pdf_store.sql": (
        # 060 creates the pdf_store table (chunk-B simplification — in-
        # Postgres ciphertext blobs instead of Supabase Storage per the
        # 2026-06-15 operator directive). Existence of the table is the
        # load-bearing artifact; the check constraints + index land in
        # the same transaction or fail it.
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='pdf_store'"
    ),
    "061_merchant_doc_flags.sql": (
        # 061 adds three columns to merchants for the document-completeness
        # checker. ``voided_check_on_file`` is the first ADD COLUMN — the
        # other two land in the same migration body or fail it together,
        # so probing on the first column is sufficient.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='voided_check_on_file'"
    ),
    "062_pdf_store_storage_path.sql": (
        # 062 relocates the pdf_store ciphertext to Supabase Storage and
        # keeps only the bucket path in the row. The load-bearing artifact
        # is the new ``storage_path`` column; the ALTER COLUMN DROP NOT
        # NULL on ciphertext/nonce + the pdf_store_blob_or_path CHECK
        # constraint land in the same transaction or fail it together.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='pdf_store' "
        "AND column_name='storage_path'"
    ),
    "063_funders_operator_status.sql": (
        # 063 adds funders.operator_status — 4-state TEXT column with a
        # NOT NULL default of 'active' and a CHECK constraint pinning
        # the allowed value set. The load-bearing artifact is the
        # column itself; the CHECK constraint and the default land in
        # the same statement and fail it together.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='funders' "
        "AND column_name='operator_status'"
    ),
    "064_merchant_context_fields.sql": (
        # 064 adds four free-text context columns on merchants for
        # Bedrock prompt injection: deal_context (operator-written),
        # close_lead_description, close_notes_summary,
        # close_call_transcripts. The four ALTER COLUMN statements land
        # in the same transaction; probing the first column suffices.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='deal_context'"
    ),
    "065_merchants_deleted_at.sql": (
        # 065 adds merchants.deleted_at (nullable timestamptz) + a partial
        # index for the soft-delete contract. The column is the load-bearing
        # artifact — the index and column land together.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='deleted_at'"
    ),
    "066_merchant_notes.sql": (
        # 066 creates the merchant_notes table (uuid pk, merchant_id fk,
        # body text NOT NULL CHECK length 1..4000, actor text, created_at
        # timestamptz default now()) + index on (merchant_id, created_at desc).
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='merchant_notes'"
    ),
    "067_merchant_web_presence.sql": (
        # 067 adds web_presence_{summary,flags,scanned_at} to merchants.
        # The three columns land in the same ADD COLUMN block; probing
        # the first (web_presence_summary) suffices.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='web_presence_summary'"
    ),
    "068_merchant_ucc.sql": (
        # 068 adds ucc_{filings,default_indicators,checked_at} to
        # merchants. Three columns in one ADD COLUMN block; probe the
        # first.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='merchants' "
        "AND column_name='ucc_filings'"
    ),
    "069_compliance_obligations.sql": (
        # 069 is the second pass on ``compliance_obligations`` — re-
        # asserts the table shape so a fresh bootstrap that never ran
        # 018 still ends up with the canonical table, and seeds the
        # 6 real obligations idempotently keyed on (state_code,
        # obligation_type). Probe the seed presence (the TX OCCC row
        # is the only one with a non-NULL next_due_date so it's a
        # safe sentinel — proves both the table and the seed pass
        # ran).
        "SELECT 1 FROM compliance_obligations "
        "WHERE state_code='TX' AND obligation_type='registration' "
        "AND next_due_date IS NOT NULL"
    ),
    "070_decisions_immutable.sql": (
        # 070 re-asserts the immutability triggers from 015 and runs the
        # cohort-2026_06 backfill. The trigger function ``block_decision_
        # modification`` is the load-bearing object — its presence proves
        # the migration's idempotent leg landed. The backfill rows
        # themselves vary by environment (prod has thousands; an empty
        # test DB has zero), so we probe the trigger function rather
        # than COUNT(decisions WHERE decided_by='backfill_2026_06') > 0.
        "SELECT 1 FROM pg_proc WHERE proname = 'block_decision_modification'"
    ),
    "072_overrides.sql": (
        # 072 extends the overrides table (017) with merchant_id,
        # document_id, pattern_false_positives, and relaxes decision_id
        # nullability — plus widens documents.parse_status to include
        # 'decline'. Probe the load-bearing new column on overrides
        # (pattern_false_positives) — its presence proves the ADD
        # COLUMN ran; the parse_status CHECK widening is paired in the
        # same migration and lands or fails atomically.
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='overrides' "
        "AND column_name='pattern_false_positives'"
    ),
}


class MigrationError(RuntimeError):
    """Base for all runner-specific failures."""


class MigrationDriftError(MigrationError):
    """A file already in schema_migrations has had its sha256 change."""


class MigrationLockHeldError(MigrationError):
    """Another runner holds the advisory lock; refuse to proceed."""


class MigrationConfigError(MigrationError):
    """Caller-side mistake: missing env var, wrong --target, etc."""


@dataclass(frozen=True)
class MigrationFile:
    filename: str
    path: Path
    sha256: str

    @classmethod
    def from_path(cls, path: Path) -> MigrationFile:
        return cls(
            filename=path.name,
            path=path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


@dataclass
class ApplyReport:
    target: str
    bootstrapped: list[str]
    applied: list[str]
    skipped: list[str]
    drift: list[str]
    pending_count: int


def _load_dotenv_local() -> None:
    """Load .env and .env.local without overwriting existing os.environ keys."""
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[MigrationFile]:
    """Return numbered migration files in lex order. Excludes _all_in_order, bootstrap."""
    if not directory.exists():
        return []
    files = [
        p
        for p in sorted(directory.iterdir())
        if p.is_file() and _MIGRATION_FILENAME_RE.match(p.name)
    ]
    return [MigrationFile.from_path(p) for p in files]


def resolve_dsn(target: str) -> str:
    env_var = _DSN_ENV_BY_TARGET.get(target)
    if env_var is None:
        raise MigrationConfigError(f"unknown target {target!r}; expected dev|staging|prod")
    dsn = os.environ.get(env_var, "").strip()
    if not dsn:
        raise MigrationConfigError(
            f"{env_var} is not set. Add it to .env.local. "
            "Get the URI from Supabase dashboard -> Settings -> Database -> "
            "Connection string (URI)."
        )
    if PROD_PROJECT_REF in dsn and target != "prod":
        raise MigrationConfigError(
            f"refusing to connect: --target={target} but DSN points at prod "
            f"project {PROD_PROJECT_REF}. Use --target prod or fix the DSN."
        )
    return dsn


def _git_short_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


class MigrationRunner:
    """Stateful orchestrator. One instance per --target invocation."""

    def __init__(
        self,
        dsn: str,
        target: str,
        actor: str,
        dry_run: bool,
        aegis_version: str,
        migrations: list[MigrationFile] | None = None,
    ) -> None:
        self.dsn = dsn
        self.target = target
        self.actor = actor
        self.dry_run = dry_run
        self.aegis_version = aegis_version
        self.migrations: list[MigrationFile] = (
            discover_migrations() if migrations is None else migrations
        )

    def run(self) -> ApplyReport:
        import psycopg

        report = ApplyReport(
            target=self.target,
            bootstrapped=[],
            applied=[],
            skipped=[],
            drift=[],
            pending_count=0,
        )

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            self._acquire_lock(conn)
            try:
                self._ensure_schema_migrations(conn)
                report.bootstrapped = self._bootstrap_if_needed(conn)
                self._apply_pending(conn, report)
            finally:
                self._release_lock(conn)
        return report

    # ------ lock ------------------------------------------------------------

    def _acquire_lock(self, conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            row = cur.fetchone()
        acquired = bool(row and row[0])
        if not acquired:
            holder_pid: object = "unknown"
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=%s LIMIT 1",
                    (ADVISORY_LOCK_KEY,),
                )
                row = cur.fetchone()
                if row:
                    holder_pid = row[0]
            raise MigrationLockHeldError(
                f"another apply_migrations run is in progress (pid={holder_pid})"
            )

    def _release_lock(self, conn: psycopg.Connection) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        except Exception as exc:
            # Connection close also releases session-scoped advisory locks,
            # so this is safe to swallow — but surface to stderr so an
            # operator can spot weird DB transport states (closed conn etc).
            # Broad except: psycopg is lazy-imported inside run(); referencing
            # psycopg.Error here would require a second local import.
            print(f"[warn] pg_advisory_unlock failed: {exc}", file=sys.stderr)

    # ------ schema_migrations bootstrap ------------------------------------

    def _ensure_schema_migrations(self, conn: psycopg.Connection) -> None:
        if self.dry_run:
            # Dry-run still needs the table to read state; create only if absent.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name='schema_migrations'"
                )
                if cur.fetchone() is None:
                    print("[dry-run] schema_migrations does not exist; would create on real run")
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    applied_by TEXT NOT NULL
                )
                """
            )

    def _bootstrap_if_needed(self, conn: psycopg.Connection) -> list[str]:
        if self.dry_run:
            # In dry-run we can still detect — useful operator preview — but
            # we never insert.
            return self._bootstrap_detect_only(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            row = cur.fetchone()
        count = int(row[0]) if row else 0
        if count > 0:
            return []

        detected: list[str] = []
        for mig in self.migrations:
            probe = MIGRATION_PROBES.get(mig.filename)
            if probe is None:
                continue
            with conn.cursor() as cur:
                cur.execute(probe)
                hit = cur.fetchone() is not None
            if hit:
                detected.append(mig.filename)

        if not detected:
            print(
                "[bootstrap] schema_migrations empty + no pre-existing schema; nothing to backfill"
            )
            return []

        print(
            f"[bootstrap] backfilling {len(detected)} pre-existing migrations as manual_pre_runner:"
        )
        for filename in detected:
            print(f"  {filename}")

        by_name = {m.filename: m for m in self.migrations}
        with conn.cursor() as cur:
            for filename in detected:
                mig = by_name[filename]
                cur.execute(
                    """
                    INSERT INTO schema_migrations (filename, sha256, applied_at, applied_by)
                    VALUES (%s, %s, NOW(), 'manual_pre_runner')
                    ON CONFLICT (filename) DO NOTHING
                    """,
                    (mig.filename, mig.sha256),
                )
        return detected

    def _bootstrap_detect_only(self, conn: psycopg.Connection) -> list[str]:
        # Check whether schema_migrations exists at all.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='schema_migrations'"
            )
            exists = cur.fetchone() is not None
        if exists:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM schema_migrations")
                row = cur.fetchone()
            if row and int(row[0]) > 0:
                return []
        detected: list[str] = []
        for mig in self.migrations:
            probe = MIGRATION_PROBES.get(mig.filename)
            if probe is None:
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(probe)
                    hit = cur.fetchone() is not None
            except Exception:
                # Probe against missing tables can throw; treat as "not present."
                hit = False
            if hit:
                detected.append(mig.filename)
        if detected:
            print(f"[dry-run] would bootstrap-backfill {len(detected)} migrations:")
            for f in detected:
                print(f"  {f}")
        return detected

    # ------ apply ----------------------------------------------------------

    def _apply_pending(self, conn: psycopg.Connection, report: ApplyReport) -> None:
        applied: dict[str, str] = {}
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT filename, sha256 FROM schema_migrations")
                rows = cur.fetchall()
            except Exception:
                # In dry-run against a DB without schema_migrations, the table
                # genuinely does not exist. Treat as empty.
                rows = []
        applied = {str(r[0]): str(r[1]) for r in rows}
        # Add bootstrap-detected entries when dry-running so they show as skipped.
        if self.dry_run:
            for f in report.bootstrapped:
                applied.setdefault(
                    f, next((m.sha256 for m in self.migrations if m.filename == f), "")
                )

        pending: list[MigrationFile] = []
        for mig in self.migrations:
            if mig.filename in applied:
                if applied[mig.filename] != mig.sha256:
                    report.drift.append(mig.filename)
                    raise MigrationDriftError(
                        f"migration {mig.filename} sha256 has changed since apply: "
                        f"stored={applied[mig.filename][:16]}... current={mig.sha256[:16]}..."
                    )
                report.skipped.append(mig.filename)
                continue
            pending.append(mig)

        report.pending_count = len(pending)
        if not pending:
            print("Nothing to apply.")
            return

        print(f"Pending: {len(pending)} migration(s)")
        for mig in pending:
            print(f"  {mig.filename}  (sha256 {mig.sha256[:12]}...)")

        if self.dry_run:
            print("\n[dry-run] not applying.")
            return

        for mig in pending:
            self._apply_one(conn, mig)
            report.applied.append(mig.filename)

    def _apply_one(self, conn: psycopg.Connection, mig: MigrationFile) -> None:
        sql_body = mig.path.read_text(encoding="utf-8")
        started = datetime.now(UTC)
        print(f"[apply] {mig.filename} ...", end="", flush=True)
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql_body)
                    finished = datetime.now(UTC)
                    cur.execute(
                        """
                        INSERT INTO schema_migrations (filename, sha256, applied_at, applied_by)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (mig.filename, mig.sha256, finished, self.actor),
                    )
                    # audit_log was created by migration 000. We deliberately
                    # write ONLY columns that exist in the migration-000 baseline
                    # — `aegis_version` lives inside the `details` JSONB (see
                    # _AUDIT_LOG_INSERT_SQL above for the full rationale).
                    cur.execute(
                        _AUDIT_LOG_INSERT_SQL,
                        (
                            self.actor,
                            mig.filename,
                            mig.sha256,
                            self.target,
                            started.isoformat(),
                            finished.isoformat(),
                            self.aegis_version,
                        ),
                    )
            elapsed_ms = int((finished - started).total_seconds() * 1000)
            print(f" OK  ({elapsed_ms} ms)")
        except Exception as exc:
            print(f" FAILED ({type(exc).__name__})")
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("dev", "staging", "prod"),
        required=True,
        help="Which environment to migrate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would apply; touch no rows.",
    )
    parser.add_argument(
        "--actor",
        help="Override the audit_log actor string (default: apply_migrations:<user>).",
    )
    args = parser.parse_args(argv)

    _load_dotenv_local()

    try:
        dsn = resolve_dsn(args.target)
    except MigrationConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    actor = args.actor or f"apply_migrations:{getpass.getuser()}"
    aegis_version = _git_short_sha()

    runner = MigrationRunner(
        dsn=dsn,
        target=args.target,
        actor=actor,
        dry_run=args.dry_run,
        aegis_version=aegis_version,
    )
    try:
        report = runner.run()
    except MigrationDriftError as exc:
        print(f"\nDRIFT: {exc}", file=sys.stderr)
        return 3
    except MigrationLockHeldError as exc:
        print(f"\nLOCK: {exc}", file=sys.stderr)
        return 4
    except MigrationConfigError as exc:
        print(f"\nCONFIG: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print()
    print(
        f"Target: {report.target}  "
        f"bootstrapped={len(report.bootstrapped)}  "
        f"applied={len(report.applied)}  "
        f"skipped={len(report.skipped)}  "
        f"pending={report.pending_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
