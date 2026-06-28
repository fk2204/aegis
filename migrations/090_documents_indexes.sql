-- Migration 090 — indexes on documents table for the dashboard hot paths.
--
-- (merchant_id, uploaded_at DESC) covers list_documents(merchant_id=...,
-- order_by uploaded_at DESC) — the attention-group + dossier doc-list
-- pulldowns. Partial index on parse_status covers the dashboard
-- attention-group + review-queue + Today-pipeline filters; partial
-- keeps it small because we never list 'failed' / 'parsed' over the
-- listing API.
--
-- Non-CONCURRENTLY: prod has ~190 doc rows; the lock is milliseconds.
-- The migration runner wraps each file in a transaction; CONCURRENTLY
-- would be incompatible. IF NOT EXISTS keeps it idempotent across
-- partial-backfill replays.

CREATE INDEX IF NOT EXISTS idx_documents_merchant_uploaded
  ON documents (merchant_id, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_parse_status
  ON documents (parse_status)
  WHERE parse_status IN ('proceed', 'manual_review', 'pending');
