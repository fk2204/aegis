-- AEGIS bootstrap.sql — applies all 8 migrations idempotently.
-- Safe to run on a fresh database OR on a partially-migrated database.
-- Generated 2026-05-10 to fix schema drift (missing byte_size column).
--
-- The trailing "schema sync" block ADDs any column that CREATE TABLE
-- IF NOT EXISTS would otherwise skip on tables that already exist with
-- an older schema.

-- ============================================================================
-- Migration 000 — foundation tables
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

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
  email TEXT,
  phone TEXT,
  zoho_deal_id TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merchants_state ON merchants (state);
CREATE INDEX IF NOT EXISTS idx_merchants_business_name ON merchants (lower(business_name));

CREATE TABLE IF NOT EXISTS documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  file_hash TEXT NOT NULL UNIQUE,
  byte_size INT NOT NULL DEFAULT 0,
  original_filename TEXT NOT NULL,
  merchant_id UUID REFERENCES merchants(id) ON DELETE SET NULL,
  parse_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (parse_status IN ('pending','proceed','review','manual_review','error')),
  fraud_score INT CHECK (fraud_score IS NULL OR fraud_score BETWEEN 0 AND 100),
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
CREATE INDEX IF NOT EXISTS idx_documents_uploaded_at ON documents (uploaded_at DESC);

CREATE TABLE IF NOT EXISTS analyses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id UUID NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  merchant_id UUID REFERENCES merchants(id) ON DELETE SET NULL,
  statement_period_start DATE NOT NULL,
  statement_period_end DATE NOT NULL,
  statement_days INT NOT NULL CHECK (statement_days >= 0),
  beginning_balance NUMERIC(14, 2) NOT NULL,
  ending_balance NUMERIC(14, 2) NOT NULL,
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

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  subject_type TEXT,
  subject_id UUID,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_log (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log (created_at DESC);

-- ============================================================================
-- Migration 001 — pgcrypto + transactions
-- ============================================================================

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

CREATE INDEX IF NOT EXISTS idx_transactions_document ON transactions (document_id);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions (merchant_id);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant_category ON transactions (merchant_id, category);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant_posted_date ON transactions (merchant_id, posted_date);

-- ============================================================================
-- Migration 002 — analyses _source_ids
-- ============================================================================

ALTER TABLE analyses
  ADD COLUMN IF NOT EXISTS avg_daily_balance_source_ids UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS true_revenue_source_ids       UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS num_nsf_source_ids            UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS days_negative_source_ids      UUID[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS mca_daily_total_source_ids    UUID[] NOT NULL DEFAULT '{}';

-- ============================================================================
-- Migration 003 — funders table
-- ============================================================================

CREATE TABLE IF NOT EXISTS funders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  active BOOLEAN NOT NULL DEFAULT true,
  min_monthly_revenue NUMERIC(14, 2),
  min_avg_daily_balance NUMERIC(14, 2),
  min_credit_score INT CHECK (min_credit_score IS NULL OR min_credit_score BETWEEN 300 AND 850),
  min_months_in_business INT CHECK (min_months_in_business IS NULL OR min_months_in_business >= 0),
  max_positions INT CHECK (max_positions IS NULL OR max_positions >= 0),
  accepts_stacking BOOLEAN NOT NULL DEFAULT false,
  min_advance NUMERIC(14, 2),
  max_advance NUMERIC(14, 2),
  max_nsf_tolerance INT CHECK (max_nsf_tolerance IS NULL OR max_nsf_tolerance >= 0),
  typical_factor_low NUMERIC(6, 4),
  typical_factor_high NUMERIC(6, 4),
  typical_holdback_low NUMERIC(6, 4),
  typical_holdback_high NUMERIC(6, 4),
  excluded_industries TEXT[] NOT NULL DEFAULT '{}',
  excluded_states TEXT[] NOT NULL DEFAULT '{}',
  guidelines_extracted_at TIMESTAMPTZ,
  guidelines_source_pdf_hash TEXT,
  notes TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_funders_active ON funders (active) WHERE active;

-- ============================================================================
-- Migration 004 — disclosure_transmission_log
-- ============================================================================

CREATE TABLE IF NOT EXISTS disclosure_transmission_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  deal_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  funder_id UUID NOT NULL REFERENCES funders(id) ON DELETE RESTRICT,
  disclosure_doc_hash TEXT NOT NULL,
  transmitted_at TIMESTAMPTZ NOT NULL,
  transmitted_to_email TEXT NOT NULL,
  merchant_acknowledged_at TIMESTAMPTZ,
  funder_notified_at TIMESTAMPTZ,
  retention_until TIMESTAMPTZ
    GENERATED ALWAYS AS (transmitted_at + INTERVAL '4 years 30 days') STORED,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dtl_deal      ON disclosure_transmission_log (deal_id);
CREATE INDEX IF NOT EXISTS idx_dtl_funder    ON disclosure_transmission_log (funder_id);
CREATE INDEX IF NOT EXISTS idx_dtl_retention ON disclosure_transmission_log (retention_until);

-- ============================================================================
-- Migrations 005, 006, 007 — funders ALTER TABLE additions
-- ============================================================================

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS requires_coj BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS aegis_compensation_disclosure_text TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS charges_merchant_advance_fees BOOLEAN NOT NULL DEFAULT false;

-- ============================================================================
-- SCHEMA SYNC — additive ALTERs for tables that may pre-exist with old schema.
-- Each ADD COLUMN IF NOT EXISTS is a no-op if the column is already correct.
-- ============================================================================

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS dba TEXT,
  ADD COLUMN IF NOT EXISTS industry_naics TEXT,
  ADD COLUMN IF NOT EXISTS industry_risk_tier TEXT,
  ADD COLUMN IF NOT EXISTS time_in_business_months INT,
  ADD COLUMN IF NOT EXISTS credit_score INT,
  ADD COLUMN IF NOT EXISTS email TEXT,
  ADD COLUMN IF NOT EXISTS phone TEXT,
  ADD COLUMN IF NOT EXISTS zoho_deal_id TEXT;

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS byte_size INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS original_filename TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS merchant_id UUID REFERENCES merchants(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS fraud_score INT,
  ADD COLUMN IF NOT EXISTS fraud_score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS all_flags TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS metadata_flags TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS error_detail TEXT,
  ADD COLUMN IF NOT EXISTS parsed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS uploaded_by TEXT NOT NULL DEFAULT 'system';

ALTER TABLE analyses
  ADD COLUMN IF NOT EXISTS monthly_revenue NUMERIC(14, 2),
  ADD COLUMN IF NOT EXISTS lowest_balance NUMERIC(14, 2),
  ADD COLUMN IF NOT EXISTS mca_positions INT,
  ADD COLUMN IF NOT EXISTS mca_daily_total NUMERIC(14, 2),
  ADD COLUMN IF NOT EXISTS debt_to_revenue NUMERIC(10, 4),
  ADD COLUMN IF NOT EXISTS payroll_detected BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS returned_ach_count INT NOT NULL DEFAULT 0;

ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS classification_confidence INT,
  ADD COLUMN IF NOT EXISTS category TEXT,
  ADD COLUMN IF NOT EXISTS running_balance NUMERIC(14, 2);

-- Done. The Supabase schema cache will refresh automatically within ~10s.
SELECT 'AEGIS bootstrap complete' AS status;
