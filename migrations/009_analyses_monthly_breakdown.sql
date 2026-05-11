-- Migration 009 — analyses.monthly_breakdown jsonb
--
-- AEGIS Accuracy v2: persist a per-calendar-month roll-up
-- (deposits, withdrawals, avg_balance) on every analysis row, so the
-- merchant-detail page and findings API can compute month-over-month
-- deltas across a renewal merchant's stack of statements without
-- re-querying transactions.
--
-- Shape: jsonb array of objects, each
--   {"month":"YYYY-MM", "deposits":"...", "withdrawals":"...", "avg_balance":"..."}
-- Decimals are stored as strings to preserve Decimal precision when
-- pydantic round-trips the row.
--
-- Nullable-default-empty so existing rows continue to work without backfill.

ALTER TABLE analyses
  ADD COLUMN IF NOT EXISTS monthly_breakdown JSONB NOT NULL DEFAULT '[]'::jsonb;
