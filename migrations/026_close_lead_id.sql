-- Migration 026 — close_lead_id integration prep (Close CRM cutover).
--
-- Adds close_lead_id to merchants (the Close CRM Lead identifier; takes
-- over the role of zoho_deal_id / zoho_lead_id going forward).
--
-- Per the operator-rule check (scripts/db_checks/merchants-zoho-id-residue.sql
-- against prod on 2026-05-20), 1 merchant row has zoho_deal_id populated and
-- 0 have zoho_lead_id. Non-zero residue triggers the rename-don't-drop path:
-- the zoho_* columns are renamed to *_archived so the historical linkage
-- stays queryable. A follow-up migration will DROP them when the operator
-- certifies no audit query needs the archived values.
--
-- Idempotency: schema_migrations gates re-runs at the application layer
-- (filename + sha256). The statements below are plain ALTER TABLE — a
-- partial re-application would error explicitly rather than silently
-- masking drift, which is the preferred fail mode.
--
-- Index design: partial UNIQUE on close_lead_id WHERE close_lead_id IS NOT
-- NULL. Multiple NULL values are allowed (legacy operator-uploaded
-- merchants without a Close linkage yet), but two merchants can't share
-- the same Close Lead.

ALTER TABLE merchants ADD COLUMN close_lead_id TEXT;

CREATE UNIQUE INDEX idx_merchants_close_lead_id
  ON merchants (close_lead_id)
  WHERE close_lead_id IS NOT NULL;

ALTER TABLE merchants RENAME COLUMN zoho_deal_id TO zoho_deal_id_archived;
ALTER TABLE merchants RENAME COLUMN zoho_lead_id TO zoho_lead_id_archived;
