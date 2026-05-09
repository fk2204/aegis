-- Migration 000 — foundation tables.
--
-- Creates the four core tables every later migration assumes exist:
--   * merchants    — one row per business AEGIS underwrites
--   * documents    — one row per uploaded PDF (file_hash, parse status, flags)
--   * analyses     — derived aggregates per parsed document; columns added by
--                    migration 002 carry _source_ids arrays back to transactions
--   * audit_log    — append-only record of every state transition
--
-- pgcrypto is created by migration 001 (kept there because 001 was the
-- first migration historically). We re-CREATE it here so 000 is also safe
-- to run standalone on a fresh database.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- merchants ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS merchants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_name TEXT NOT NULL,
  dba TEXT,
  owner_name TEXT NOT NULL,
  state CHAR(2) NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
  industry_naics TEXT,
  industry_risk_tier TEXT CHECK (industry_risk_tier IS NULL
    OR industry_risk_tier IN ('low','moderate','elevated','high','avoid')),
  time_in_business_months INT CHECK (time_in_business_months IS NULL
    OR time_in_business_months >= 0),
  credit_score INT CHECK (credit_score IS NULL
    OR credit_score BETWEEN 300 AND 850),

  -- Contact (PII — do not log)
  email TEXT,
  phone TEXT,

  -- Idempotency for Zoho sync (Phase 5). UNIQUE so ON CONFLICT works.
  zoho_deal_id TEXT UNIQUE,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merchants_state ON merchants (state);
CREATE INDEX IF NOT EXISTS idx_merchants_business_name
  ON merchants (lower(business_name));

-- documents ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Hash of the file bytes (sha256 hex). UNIQUE so re-upload of the same
  -- PDF is detected at insert time without re-parsing.
  file_hash TEXT NOT NULL UNIQUE,

  byte_size INT NOT NULL CHECK (byte_size > 0),

  -- The original filename is recorded for the audit trail but NEVER used
  -- in any filesystem path operation (per CLAUDE.md security rule).
  original_filename TEXT NOT NULL,

  merchant_id UUID REFERENCES merchants(id) ON DELETE SET NULL,

  parse_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (parse_status IN ('pending','proceed','review','manual_review','error')),

  fraud_score INT CHECK (fraud_score IS NULL
    OR fraud_score BETWEEN 0 AND 100),
  fraud_score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
  all_flags TEXT[] NOT NULL DEFAULT '{}',
  metadata_flags TEXT[] NOT NULL DEFAULT '{}',

  error_detail TEXT,

  uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  parsed_at TIMESTAMPTZ,
  uploaded_by TEXT NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (parse_status);
CREATE INDEX IF NOT EXISTS idx_documents_merchant ON documents (merchant_id);
CREATE INDEX IF NOT EXISTS idx_documents_uploaded_at
  ON documents (uploaded_at DESC);

-- analyses -------------------------------------------------------------------
-- Derived aggregates per document. Migration 002 adds *_source_ids UUID[]
-- columns so every aggregate is traceable to specific transaction rows.

CREATE TABLE IF NOT EXISTS analyses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id UUID NOT NULL UNIQUE
    REFERENCES documents(id) ON DELETE CASCADE,
  merchant_id UUID REFERENCES merchants(id) ON DELETE SET NULL,

  statement_period_start DATE NOT NULL,
  statement_period_end DATE NOT NULL,
  statement_days INT NOT NULL CHECK (statement_days >= 0),

  beginning_balance NUMERIC(14, 2) NOT NULL,
  ending_balance NUMERIC(14, 2) NOT NULL,

  -- Aggregates with source attribution (migration 002 mirrors these into
  -- their *_source_ids columns).
  avg_daily_balance NUMERIC(14, 2) NOT NULL,
  true_revenue NUMERIC(14, 2) NOT NULL,
  monthly_revenue NUMERIC(14, 2) NOT NULL,
  lowest_balance NUMERIC(14, 2) NOT NULL,
  num_nsf INT NOT NULL CHECK (num_nsf >= 0),
  days_negative INT NOT NULL CHECK (days_negative >= 0),
  mca_positions INT NOT NULL CHECK (mca_positions >= 0),
  mca_daily_total NUMERIC(14, 2) NOT NULL,
  debt_to_revenue NUMERIC(10, 4) NOT NULL CHECK (debt_to_revenue >= 0),
  payroll_detected BOOLEAN NOT NULL DEFAULT false,
  returned_ach_count INT NOT NULL DEFAULT 0 CHECK (returned_ach_count >= 0),

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analyses_merchant ON analyses (merchant_id);

-- audit_log ------------------------------------------------------------------
-- Append-only. Every state transition writes here. Audit-write failures
-- FAIL the calling operation per CLAUDE.md (no silent log-and-continue).

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  subject_type TEXT,
  subject_id UUID,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_subject
  ON audit_log (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_audit_created_at
  ON audit_log (created_at DESC);
