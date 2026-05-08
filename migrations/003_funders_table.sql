-- Migration 003 — funders table.
--
-- Mirrors `aegis.funders.models.FunderRow`. Uniqueness on `name` is the
-- application-level invariant the matcher and repository assume.
--
-- pgcrypto extension is created in migration 001; gen_random_uuid() is
-- already available.

CREATE TABLE IF NOT EXISTS funders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  active BOOLEAN NOT NULL DEFAULT true,

  -- Hard gates
  min_monthly_revenue NUMERIC(14, 2),
  min_avg_daily_balance NUMERIC(14, 2),
  min_credit_score INT CHECK (min_credit_score IS NULL
    OR min_credit_score BETWEEN 300 AND 850),
  min_months_in_business INT CHECK (min_months_in_business IS NULL
    OR min_months_in_business >= 0),
  max_positions INT CHECK (max_positions IS NULL OR max_positions >= 0),
  accepts_stacking BOOLEAN NOT NULL DEFAULT false,
  min_advance NUMERIC(14, 2),
  max_advance NUMERIC(14, 2),
  max_nsf_tolerance INT CHECK (max_nsf_tolerance IS NULL
    OR max_nsf_tolerance >= 0),

  -- Pricing envelope (informational)
  typical_factor_low NUMERIC(6, 4),
  typical_factor_high NUMERIC(6, 4),
  typical_holdback_low NUMERIC(6, 4),
  typical_holdback_high NUMERIC(6, 4),

  -- Exclusions (lowercased tokens / two-letter USPS codes)
  excluded_industries TEXT[] NOT NULL DEFAULT '{}',
  excluded_states TEXT[] NOT NULL DEFAULT '{}',

  -- Provenance — when did the latest LLM extraction run + against which PDF.
  guidelines_extracted_at TIMESTAMPTZ,
  guidelines_source_pdf_hash TEXT,

  notes TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_funders_active ON funders (active) WHERE active;
