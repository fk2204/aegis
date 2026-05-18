-- Migration 016 — disclosures table (master plan §9.3)
--
-- One row per rendered + delivered disclosure document. Lives alongside
-- the existing `disclosure_transmission_log` (migration 004) which
-- captures only the transmission event; this table additionally captures
-- the rendered artifact itself + the inputs + the snapshot-locked
-- template SHA256. Snapshot test fails if template_sha256 drifts.
--
-- decision_id references the immutable decisions row that produced the
-- disclosure obligation. Tracking back to decisions allows the audit
-- view to render "this decision generated this disclosure" without a
-- separate join table.
--
-- merchant_signature_* fields populate when the merchant acknowledges
-- the disclosure (e-sign or wet signature stored as hash). Inputs are
-- the exact field values fed to the Jinja template — JSONB so we can
-- reproduce the rendered PDF from this row alone, no upstream query
-- needed.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS disclosures (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  deal_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  decision_id UUID NOT NULL REFERENCES decisions(id) ON DELETE RESTRICT,
  state_code TEXT NOT NULL,
  template_path TEXT NOT NULL,
  template_sha256 TEXT NOT NULL,
  disclosure_type TEXT NOT NULL
    CHECK (disclosure_type IN ('origination', 'renewal', 'defensive')),
  inputs JSONB NOT NULL,
  rendered_pdf_path TEXT NOT NULL,
  rendered_pdf_sha256 TEXT NOT NULL,
  delivered_at TIMESTAMPTZ,
  delivery_method TEXT,
  merchant_signature_at TIMESTAMPTZ,
  merchant_signature_ip TEXT,
  merchant_signature_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_disclosures_deal ON disclosures (deal_id);
CREATE INDEX IF NOT EXISTS idx_disclosures_decision ON disclosures (decision_id);
CREATE INDEX IF NOT EXISTS idx_disclosures_state ON disclosures (state_code);
CREATE INDEX IF NOT EXISTS idx_disclosures_created ON disclosures (created_at DESC);

ALTER TABLE disclosures ENABLE ROW LEVEL SECURITY;
