-- Migration 073 — evolutionary ALTER on the existing processor_statements
-- table (migration 020).
--
-- Why ALTER, not CREATE
-- ---------------------
-- Migration 020 already created ``processor_statements`` with the
-- validator-shape column set (processor / gross_volume / refunds_total /
-- chargebacks_total / fees_total / payouts_total / net_revenue /
-- transaction_count / refund_count / chargeback_count / source_ids /
-- validation_passed / validation_failures / parse_status). A first draft
-- of 073 attempted to re-create the table with the dossier-shape
-- column names (processor_type / total_gross_volume / ...) — on prod
-- where 020 already shipped, IF NOT EXISTS makes 073 a no-op and the
-- ``SupabaseProcessorStatementRepository`` writes would 400 on every
-- upsert (Postgres rejects the new column names that don't exist on the
-- 020-shaped table). The non-destructive forward fix is to ALTER 020 in
-- place: keep the existing column names, widen the CHECK so the new
-- processors are accepted, add the four columns the dossier needs, and
-- DROP NOT NULL on the columns the new write path doesn't carry.
--
-- What this migration does
-- ------------------------
-- 1. WIDEN the CHECK constraint on ``processor`` to accept
--    ``toast / clover / paypal`` in addition to ``stripe / square``.
--    DROP CONSTRAINT + ADD CONSTRAINT pattern — no data move.
-- 2. ADD the four dossier-shape columns:
--      * avg_daily_volume NUMERIC(14,2)  — capacity sizing for funders
--      * refund_rate      NUMERIC(6,4)   — rate math (4dp precision)
--      * parse_method     TEXT           — discriminator csv / pdf_vision
--      * raw_line_items   JSONB          — per-row forensic-replay payload
--    Each ALTER uses ADD COLUMN IF NOT EXISTS so re-applying the
--    migration is idempotent (matches the rest of the migration set).
-- 3. ADD a UNIQUE constraint on ``document_id`` — backs the upsert
--    ON CONFLICT path in ``SupabaseProcessorStatementRepository.upsert``.
--    A processor statement parses one document into one aggregate row;
--    re-parses replace via the unique conflict semantics. Wrapped in a
--    DO block + duplicate_object exception so re-applying is idempotent.
-- 4. DROP NOT NULL on the columns the new dossier-shape write path does
--    NOT carry. This is the load-bearing compatibility step — the
--    ``StripeDossierAggregates``-driven repo only populates a subset of
--    020's columns; without dropping NOT NULL, every upsert from the new
--    code path would fail with ``null value in column ...`` errors.
--      * period_start / period_end  — dossier-shape rows can land before
--        the printed period is captured (e.g. CSV exports compute it
--        from row dates).
--      * refunds_total / chargebacks_total — the dossier flow surfaces
--        chargeback_count + refund_rate instead of carrying these
--        denormalised totals. The repo defaults to 0 in the payload
--        when the value is not on the row.
--      * fees_total / payouts_total — same; mapped from the dossier
--        ``total_fees`` / ``total_payouts`` when the repo carries them.
--      * net_revenue — mapped from the dossier ``total_net_volume``.
--      * transaction_count / refund_count — the dossier carries
--        ``charge_count`` / ``refund_count`` on the aggregates but the
--        repo doesn't persist them as table columns yet. Defaulted via
--        the existing column DEFAULT 0.
--      * gross_volume — mapped from the dossier ``total_gross_volume``.
--      * validation_passed / parse_status — the new write path only
--        upserts on a successful parse, so these are implicitly true /
--        ``proceed``. Left nullable so a follow-up that backfills the
--        validator outcome can populate them.
-- 5. source_ids stays NOT NULL DEFAULT '{}'::jsonb — the repository
--    populates the per-metric source-id lists on every write (AEGIS
--    auditability rule: every aggregate carries its contributing
--    transaction IDs).
--
-- What this migration does NOT do
-- -------------------------------
-- * It does NOT rename existing columns (``processor`` →
--   ``processor_type``, ``gross_volume`` → ``total_gross_volume``).
--   Renames on a live table risk write breakage in any code path that
--   still reads the old names. The repository encoder bridges the
--   naming gap in Python.
-- * It does NOT drop existing columns. Future cleanup of redundant
--   denormalised fields (refunds_total, chargebacks_total,
--   transaction_count, refund_count) lives in a separate migration once
--   no caller depends on them.
--
-- RLS: already enabled by migration 020. No-op here.

BEGIN;

-- Step 1 — widen the processor CHECK to accept the additional brands
-- the new repository allows. Postgres requires DROP + ADD; there is no
-- ALTER CONSTRAINT for CHECK widening.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.table_constraints
    WHERE table_schema = 'public'
      AND table_name = 'processor_statements'
      AND constraint_name = 'processor_statements_processor_check'
  ) THEN
    ALTER TABLE processor_statements
      DROP CONSTRAINT processor_statements_processor_check;
  END IF;
END
$$;

ALTER TABLE processor_statements
  ADD CONSTRAINT processor_statements_processor_check
    CHECK (processor IN ('stripe', 'square', 'toast', 'clover', 'paypal'));

-- Step 2 — add the four dossier-shape columns. IF NOT EXISTS keeps
-- the migration idempotent if a partial re-run lands.
ALTER TABLE processor_statements
  ADD COLUMN IF NOT EXISTS avg_daily_volume NUMERIC(14, 2);
ALTER TABLE processor_statements
  ADD COLUMN IF NOT EXISTS refund_rate NUMERIC(6, 4);
ALTER TABLE processor_statements
  ADD COLUMN IF NOT EXISTS parse_method TEXT;
ALTER TABLE processor_statements
  ADD COLUMN IF NOT EXISTS raw_line_items JSONB;

-- Step 3 — UNIQUE(document_id). Wrapped in a DO block so re-applying
-- the migration doesn't raise duplicate_object.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'processor_statements_document_id_key'
  ) THEN
    ALTER TABLE processor_statements
      ADD CONSTRAINT processor_statements_document_id_key UNIQUE (document_id);
  END IF;
END
$$;

-- Step 4 — DROP NOT NULL on the columns the dossier-shape write path
-- does not carry. The columns themselves stay (no data loss); the
-- constraint is lifted so the new upsert path can land rows that
-- populate only the dossier-relevant subset.
ALTER TABLE processor_statements ALTER COLUMN period_start DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN period_end DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN gross_volume DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN refunds_total DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN chargebacks_total DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN fees_total DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN payouts_total DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN net_revenue DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN validation_passed DROP NOT NULL;
ALTER TABLE processor_statements ALTER COLUMN parse_status DROP NOT NULL;

-- Audit row for the schema change. One row per migration — matches the
-- 070 / 072 / 074 pattern.
INSERT INTO audit_log (actor, action, subject_type, subject_id, details)
VALUES (
  'migration_073',
  'processor_statements.schema_altered',
  'table',
  NULL,
  jsonb_build_object(
    'columns_added', jsonb_build_array(
      'avg_daily_volume', 'refund_rate', 'parse_method', 'raw_line_items'
    ),
    'check_widened', 'processor IN (stripe, square, toast, clover, paypal)',
    'unique_constraint_added', 'document_id',
    'not_null_dropped', jsonb_build_array(
      'period_start', 'period_end', 'gross_volume', 'refunds_total',
      'chargebacks_total', 'fees_total', 'payouts_total', 'net_revenue',
      'validation_passed', 'parse_status'
    ),
    'base_migration', '020_processor_statements.sql'
  )
);

COMMIT;
