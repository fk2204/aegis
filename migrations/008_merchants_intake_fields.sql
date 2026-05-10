-- Migration 008 — merchants intake fields.
--
-- Phase 7 dashboard v1 expands the operator intake form: entity type,
-- EIN, requested terms, broker source, intake date, renewal flag, and a
-- preferred-funder pointer (the UI button to set this is deferred; the
-- column lands now so the deferred UI is a no-migration patch later).
--
-- All new columns are nullable / default safely so existing merchant rows
-- need no backfill.

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS entity_type TEXT
    CHECK (entity_type IS NULL
      OR entity_type IN ('llc','corp','sole_prop','partnership','other')),
  ADD COLUMN IF NOT EXISTS ein TEXT,
  ADD COLUMN IF NOT EXISTS requested_amount NUMERIC(14,2)
    CHECK (requested_amount IS NULL OR requested_amount >= 0),
  ADD COLUMN IF NOT EXISTS requested_factor NUMERIC(6,4)
    CHECK (requested_factor IS NULL OR requested_factor > 0),
  ADD COLUMN IF NOT EXISTS requested_term_days INT
    CHECK (requested_term_days IS NULL OR requested_term_days > 0),
  ADD COLUMN IF NOT EXISTS broker_source TEXT,
  ADD COLUMN IF NOT EXISTS intake_date DATE,
  ADD COLUMN IF NOT EXISTS is_renewal BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS preferred_funder_id UUID REFERENCES funders(id);

-- Index for the operator dashboard's "deals by intake date" view (planned
-- but deferred). Cheap to add now.
CREATE INDEX IF NOT EXISTS idx_merchants_intake_date
  ON merchants (intake_date DESC);
