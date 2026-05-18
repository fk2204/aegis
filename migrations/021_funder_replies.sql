-- Migration 021 — funder_replies table (mp Phase 10 / Stage 2D-main)
--
-- Captures every funder reply (email or operator-paste) tied to a deal.
-- One row per inbound reply; the body is preserved so re-parse is
-- possible if the LLM extractor improves. The terms_json blob holds
-- the structured fields the parser extracted (amount, factor, term,
-- holdback, etc.) when the reply contained an offer.
--
-- Outcome stamping per the crystalline-swimming-twilight plan
-- refinement (5):
--   - A funder reply matching a deal with an UNSTAMPED override
--     stamps that override (sets outcome + outcome_recorded_at).
--   - A funder reply matching a deal with an already-stamped
--     override persists the reply but does NOT overwrite the stamp.
--   - A funder reply arriving BEFORE its override is persisted
--     here; at override-creation time the most-recent matching
--     reply stamps the override.
-- Idempotency lives in application code (aegis.funders.replies);
-- the DB only stores the rows.
--
-- source_email_sha256 is the SHA-256 of the canonical raw_text. Lets
-- the operator-paste path detect "is this the same email the webhook
-- already ingested?" without storing PII in a way that survives the
-- hashed row.
--
-- parsed_confidence is the LLM's own 0-100 estimate of how confident
-- it was in the structured extraction. Low values surface in the
-- dashboard so the operator can hand-correct before the override is
-- stamped.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS funder_replies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  deal_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  funder_id UUID NOT NULL REFERENCES funders(id) ON DELETE RESTRICT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Funder's response classification. The validation gate
  -- (aegis.funders.replies.validate_reply) rejects rows where the
  -- math doesn't reconcile (amount * factor != payback +/- $0.01)
  -- for status='approved'; declined/countered replies don't require
  -- math reconciliation.
  status TEXT NOT NULL
    CHECK (status IN ('approved', 'declined', 'countered')),

  -- Structured terms when the reply contained an offer. Shape:
  --   { amount: "20000.00",
  --     factor: "1.32",
  --     payback: "26400.00",
  --     term_days: 120,
  --     daily_payment: "220.00",
  --     holdback_pct: "0.12" }
  -- Sparse — only fields the parser confidently extracted.
  terms_json JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- SHA-256 of the canonical raw_text. Idempotency aid for the
  -- operator-paste path (operator pastes the same email twice ->
  -- detect duplicate via hash). NOT a unique constraint because
  -- funders sometimes re-send the same message; the audit log
  -- preserves each ingestion attempt.
  source_email_sha256 TEXT,

  -- LLM's self-reported confidence in the structured extraction.
  -- 0-100; surfaced in the dashboard for operator review when low.
  parsed_confidence INT CHECK (parsed_confidence BETWEEN 0 AND 100),

  -- The raw email body (or paste). PII-bearing. RLS denies anon;
  -- service_role can read for re-parse / auditor review.
  raw_text TEXT NOT NULL,

  -- Where the reply came from. The webhook route writes 'webhook';
  -- the operator-paste dashboard endpoint writes 'operator_paste'.
  ingested_via TEXT NOT NULL
    CHECK (ingested_via IN ('webhook', 'operator_paste')),

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_funder_replies_deal ON funder_replies (deal_id);
CREATE INDEX IF NOT EXISTS idx_funder_replies_funder ON funder_replies (funder_id);
CREATE INDEX IF NOT EXISTS idx_funder_replies_received
  ON funder_replies (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_funder_replies_deal_funder
  ON funder_replies (deal_id, funder_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_funder_replies_hash
  ON funder_replies (source_email_sha256)
  WHERE source_email_sha256 IS NOT NULL;

ALTER TABLE funder_replies ENABLE ROW LEVEL SECURITY;
