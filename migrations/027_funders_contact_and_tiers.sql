-- Migration 027 — funders contact + tier structure.
--
-- Adds four contact fields, a JSONB `tiers` array, and two text[]
-- bullet-list columns (auto-decline conditions, conditional
-- requirements) to back the funder-detail-page redesign:
--   * Contact card — top of page, replaces info buried in notes prose.
--   * Tiers table — structured rows replace tier prose in notes.
--   * Auto-decline / Conditional requirements — two-column bullets.
--
-- Backward compatible: every new column has a default (empty string,
-- '{}' array, or '[]'::jsonb array). Existing rows persist with their
-- notes prose unchanged. Per-funder re-extraction (operator-supervised,
-- step F of the UI-redesign chain) populates the new structured fields.

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS contact_name             TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS contact_phone            TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS contact_email            TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS submission_email         TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS tiers                    JSONB  NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS auto_decline_conditions  TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS conditional_requirements TEXT[] NOT NULL DEFAULT '{}';
