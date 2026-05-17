-- Migration 014 — analyses.bank_name + analyses.account_last4
--
-- Persists the bank identity that pass-1 extraction already pulls into
-- StatementSummary (`bank_name`, `account_last4`). Today those fields
-- vanish after the validation gate runs; bundling needs them durable so
-- the merchant detail page can group statements into per-account bundles
-- and surface coverage gaps between earliest and latest period.
--
-- Both columns are nullable for backfill safety: pre-existing rows show
-- as a single "bank not detected — bundle unverified" bundle keyed on
-- (NULL, NULL). The composite index targets the bundling query path
-- `analyses WHERE merchant_id = ? AND bank_name = ? AND account_last4 = ?`
-- so the dashboard pulls the active bundle in one hit, not N.

ALTER TABLE analyses
  ADD COLUMN IF NOT EXISTS bank_name TEXT,
  ADD COLUMN IF NOT EXISTS account_last4 CHAR(4);

CREATE INDEX IF NOT EXISTS idx_analyses_merchant_bank
  ON analyses (merchant_id, bank_name, account_last4);
