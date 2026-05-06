-- Migration 001 — pgcrypto extension + transactions table.
--
-- pgcrypto provides gen_random_uuid() on Supabase Postgres. Every later
-- migration that uses gen_random_uuid() for DEFAULT values relies on this
-- extension being created here.
--
-- The `transactions` table is the audit-trail backbone: each row is a
-- single line item from a parsed PDF, with `source_page` + `source_line`
-- pointing back at the bank statement. Aggregate columns on `analyses`
-- carry arrays of these UUIDs (see migration 002) so any aggregate metric
-- can be drilled back to its contributing rows.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  merchant_id UUID REFERENCES merchants(id) ON DELETE SET NULL,
  posted_date DATE NOT NULL,
  description TEXT NOT NULL,
  amount NUMERIC(14, 2) NOT NULL,
  running_balance NUMERIC(14, 2),
  source_page INT NOT NULL CHECK (source_page >= 1),
  source_line INT NOT NULL CHECK (source_line >= 1),
  category TEXT,
  classification_confidence INT CHECK (classification_confidence BETWEEN 0 AND 100),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_document
  ON transactions (document_id);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant
  ON transactions (merchant_id);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant_category
  ON transactions (merchant_id, category);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant_posted_date
  ON transactions (merchant_id, posted_date);
