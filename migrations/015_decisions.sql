-- Migration 015 — decisions table (master plan §9.2)
--
-- Immutable decision snapshot. Every approve / decline / manual_review
-- writes one row here BEFORE the operation returns. The row is the
-- regulator-defense artifact: it freezes everything an auditor or counsel
-- needs to answer "why did you decline Merchant X on 2026-03-14" — score
-- factors at decision time, contributing transaction UUIDs, the bank-
-- statement PDF SHA256, the OFAC cache timestamp + hash, the rule pack
-- version, and the disclosure template SHA256 (if a disclosure was issued).
--
-- Master plan §9.2 spec calls for `deal_id uuid NOT NULL REFERENCES deals(id)`.
-- AEGIS does not have a `deals` table — `deals` is a view materialized
-- from `documents` (migration 012). The deal_id surfaced to operators is a
-- composite string `merchant_id:document_id` (aegis.deals.models.format_deal_id).
-- Per the existing pattern in `submissions` (migration 013) and
-- `disclosure_transmission_log` (migration 004), `deal_id` here references
-- `documents(id)` directly. The composite deal_id is reconstructible from
-- the joined documents.merchant_id; no information is lost.
--
-- Backfill_quality column (per crystalline-swimming-twilight plan, refinement 4):
--   NULL      → live decisions (default; reserved for non-backfill rows)
--   'partial' → backfilled rows with analysis_id + contributing_transaction_uuids
--               + bank_statement_pdf_sha256 + (score OR decision) populated
--   'minimal' → backfilled rows with only deal_id + decision populated
--   'full'    → reserved; NEVER set on backfill rows
--
-- Immutability: two triggers block UPDATE and DELETE so the table is
-- append-only by construction. A superseding decision is a NEW row with
-- a later decided_at; the prior row stays as evidence of what was
-- decided at the time.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS decisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  deal_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  decided_by TEXT NOT NULL,

  decision TEXT NOT NULL
    CHECK (decision IN ('approve', 'decline', 'manual_review', 'redisclosure')),
  decision_reason_codes TEXT[] NOT NULL,
  score NUMERIC(5, 2),
  score_factors JSONB NOT NULL,

  analysis_id UUID REFERENCES analyses(id) ON DELETE RESTRICT,
  contributing_transaction_uuids UUID[] NOT NULL DEFAULT '{}',
  bank_statement_pdf_sha256 TEXT,

  state_code TEXT NOT NULL,
  cfdl_tier INT NOT NULL,
  disclosure_template_path TEXT,
  disclosure_template_sha256 TEXT,
  disclosure_pdf_sha256 TEXT,
  apr_calculated NUMERIC(8, 4),
  apr_method TEXT,

  ofac_cache_timestamp TIMESTAMPTZ,
  ofac_cache_sha256 TEXT,

  aegis_version TEXT NOT NULL,
  rule_pack_version TEXT NOT NULL,

  backfill_quality TEXT NULL
    CHECK (backfill_quality IS NULL
        OR backfill_quality IN ('minimal', 'partial', 'full'))
);

CREATE INDEX IF NOT EXISTS idx_decisions_deal ON decisions (deal_id);
CREATE INDEX IF NOT EXISTS idx_decisions_decided_at ON decisions (decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_decided_by ON decisions (decided_by);

-- Partial unique index for idempotent backfill: at most one
-- backfill_2026_05 row per deal_id. Live decisions can stack freely
-- (each supersedes the prior) — this index ONLY constrains backfill.
CREATE UNIQUE INDEX IF NOT EXISTS uq_decisions_backfill_per_deal
  ON decisions (deal_id)
  WHERE decided_by = 'backfill_2026_05';

-- Immutability triggers (master plan §9.2 verbatim).
CREATE OR REPLACE FUNCTION block_decision_modification() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'decisions table is append-only; use a new row to supersede';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decisions_no_update ON decisions;
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
  FOR EACH ROW EXECUTE FUNCTION block_decision_modification();

DROP TRIGGER IF EXISTS decisions_no_delete ON decisions;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
  FOR EACH ROW EXECUTE FUNCTION block_decision_modification();

-- Match migration 011 — service_role bypasses RLS; anon/authenticated
-- are denied. No policies = full deny for PostgREST.
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
