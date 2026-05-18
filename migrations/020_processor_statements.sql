-- Migration 020 — processor_statements table (mp Phase 6.6 / Stage 2C)
--
-- Stores the parsed output of a payment-processor statement (Stripe,
-- Square, ...). Sibling to ``analyses`` (which holds bank-statement
-- parse aggregates); processor statements have a different shape so
-- they live in their own table rather than being shoehorned into the
-- bank schema.
--
-- One row per uploaded processor PDF. The same documents table holds
-- the source PDF reference for both bank and processor statements;
-- ``processor`` discriminates.

CREATE TABLE IF NOT EXISTS processor_statements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  merchant_id UUID REFERENCES merchants(id) ON DELETE SET NULL,
  processor TEXT NOT NULL CHECK (processor IN ('stripe', 'square')),

  -- Statement period (printed on the document).
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,

  -- Money totals. numeric(14,2) per CLAUDE.md (never float8).
  gross_volume NUMERIC(14, 2) NOT NULL,
  refunds_total NUMERIC(14, 2) NOT NULL,
  chargebacks_total NUMERIC(14, 2) NOT NULL,
  fees_total NUMERIC(14, 2) NOT NULL,
  payouts_total NUMERIC(14, 2) NOT NULL,
  net_revenue NUMERIC(14, 2) NOT NULL,

  -- Counts.
  transaction_count INT NOT NULL DEFAULT 0,
  refund_count INT NOT NULL DEFAULT 0,
  chargeback_count INT NOT NULL DEFAULT 0,

  -- Audit trail. The source row UUIDs that contributed to each
  -- aggregate live in the JSON. Same _source_ids[] discipline as
  -- analyses — every metric must be traceable back to the line items
  -- that produced it.
  source_ids JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Validation gate outcome:
  --   passed   = gross − refunds − chargebacks − fees == payouts ± $0.01
  --   failed   = math gap too large → document routes to manual_review
  validation_passed BOOLEAN NOT NULL,
  validation_failures TEXT[] NOT NULL DEFAULT '{}',

  -- Parse status mirrors analyses semantics:
  --   proceed       = clean parse, gate passed
  --   review        = soft concerns surfaced
  --   manual_review = hard fail / gate broken
  parse_status TEXT NOT NULL
    CHECK (parse_status IN ('proceed', 'review', 'manual_review')),

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_processor_statements_document
  ON processor_statements (document_id);
CREATE INDEX IF NOT EXISTS idx_processor_statements_merchant
  ON processor_statements (merchant_id);
CREATE INDEX IF NOT EXISTS idx_processor_statements_processor
  ON processor_statements (processor);

-- RLS: same posture as the rest of the tables — service_role bypasses,
-- no policies = full deny for PostgREST. See migration 011.
ALTER TABLE processor_statements ENABLE ROW LEVEL SECURITY;
