-- Migration 008 — merchants intake fields.
--
-- Phase 7 dashboard v1 expands the operator intake form: entity type,
-- EIN, requested terms, broker source, intake date, renewal flag, and a
-- preferred-funder pointer (the UI button to set this is deferred; the
-- column lands now so the deferred UI is a no-migration patch later).
--
-- All new columns are nullable / default safely so existing merchant rows
-- need no backfill.
--
-- 2026-05-10 update: split into one-column-per-ALTER so Supabase's
-- planner doesn't bundle-validate against unrelated triggers/RLS
-- policies on the table. Each statement is idempotent.

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS entity_type TEXT
    CHECK (entity_type IS NULL
      OR entity_type IN ('llc','corp','sole_prop','partnership','other'));

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS ein TEXT;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS requested_amount NUMERIC(14,2)
    CHECK (requested_amount IS NULL OR requested_amount >= 0);

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS requested_factor NUMERIC(6,4)
    CHECK (requested_factor IS NULL OR requested_factor > 0);

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS requested_term_days INT
    CHECK (requested_term_days IS NULL OR requested_term_days > 0);

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS broker_source TEXT;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS intake_date DATE;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS is_renewal BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS preferred_funder_id UUID REFERENCES funders(id);

-- Index for the operator dashboard's "deals by intake date" view (planned
-- but deferred). Cheap to add now.
CREATE INDEX IF NOT EXISTS idx_merchants_intake_date
  ON merchants (intake_date DESC);
