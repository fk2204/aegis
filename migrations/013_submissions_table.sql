-- Migration 013 — submissions table (DESIGN ONLY — NOT YET APPLIED).
--
-- One row per (merchant, document, funder) tuple capturing the
-- funder-facing CSV submission AEGIS produced (see
-- src/aegis/scoring/submission_package.py). Phase 7C centerpiece —
-- replaces the Pydantic-only ``merchant.submitted_to_funder_ids`` /
-- ``last_submitted_at`` fields that reset on a Supabase round-trip
-- (see merchants/models.py lines 81-83).
--
-- Why no separate deal_id column:
--   F1 of the Phase 7 audit locks "deal is a (merchant_id, document_id)
--   derived view, no new table." The natural key here is
--   (merchant_id, document_id, funder_id) — the deal_id is materialized
--   by application code via aegis.deals.models.format_deal_id when the
--   caller needs the composite string.
--
-- Status transitions (enforced application-side in
--   aegis.submissions.repository):
--     submitted          (initial — CSV produced and forwarded)
--       → funder_declined
--       → funder_approved → funded
--       → withdrawn       (operator cancels before funder decides)
--
-- ``csv_doc_hash`` records sha256 of the exact CSV bytes that went out
-- so a future regulator question ("what did you send the funder?") is
-- answerable from this row alone — mirrors disclosure_transmission_log's
-- ``disclosure_doc_hash`` (migration 004 line 32).

CREATE TABLE IF NOT EXISTS submissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Deal components. ON DELETE RESTRICT — a submission record is a
  -- compliance artifact and must outlive merchant or document deletion.
  merchant_id UUID NOT NULL REFERENCES merchants(id) ON DELETE RESTRICT,
  document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  funder_id   UUID NOT NULL REFERENCES funders(id)   ON DELETE RESTRICT,

  -- Submission act
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  submitted_by TEXT NOT NULL,                 -- operator email / "system"
  csv_doc_hash TEXT NOT NULL,                 -- sha256 hex of the CSV bytes
  csv_filename TEXT NOT NULL,                 -- e.g. acme__quick-capital.csv

  -- Snapshot of the proposed terms at submission time (Decimal-safe via
  -- numeric). Phase-7C feature: enables P&L roll-ups without re-running
  -- scoring against drifted statement aggregates.
  proposed_amount   NUMERIC(14, 2) NOT NULL,
  proposed_factor   NUMERIC(6, 4)  NOT NULL CHECK (proposed_factor > 1
                                                AND proposed_factor <= 2),
  proposed_holdback NUMERIC(6, 4)  NOT NULL CHECK (proposed_holdback >= 0
                                                AND proposed_holdback <= 1),

  -- Lifecycle
  status TEXT NOT NULL DEFAULT 'submitted'
    CHECK (status IN ('submitted',
                      'funder_declined',
                      'funder_approved',
                      'funded',
                      'withdrawn')),
  funder_response_at   TIMESTAMPTZ,
  funder_response_note TEXT,

  -- Funded leg (NULL until status='funded')
  funded_amount NUMERIC(14, 2) CHECK (funded_amount IS NULL
                                   OR funded_amount > 0),
  factor_rate   NUMERIC(6, 4)  CHECK (factor_rate IS NULL
                                   OR (factor_rate > 1 AND factor_rate <= 2)),
  funded_at     TIMESTAMPTZ,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Lifecycle invariants enforced at the DB layer:
  --   * funded_amount + factor_rate + funded_at all-or-nothing
  --   * funded_at only valid when status='funded'
  CONSTRAINT submissions_funded_fields_together CHECK (
    (funded_amount IS NULL AND factor_rate IS NULL AND funded_at IS NULL)
    OR
    (funded_amount IS NOT NULL AND factor_rate IS NOT NULL
       AND funded_at IS NOT NULL AND status = 'funded')
  )
);

-- Natural-key uniqueness: one submission per (merchant, document,
-- funder) tuple. A re-submission to the same funder is an UPDATE.
CREATE UNIQUE INDEX IF NOT EXISTS uq_submissions_deal_funder
  ON submissions (merchant_id, document_id, funder_id);

CREATE INDEX IF NOT EXISTS idx_submissions_funder ON submissions (funder_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions (status);
CREATE INDEX IF NOT EXISTS idx_submissions_merchant
  ON submissions (merchant_id);
CREATE INDEX IF NOT EXISTS idx_submissions_submitted_at
  ON submissions (submitted_at DESC);

-- Match migration 011 — service_role bypasses RLS; anon/authenticated
-- are denied. No policies = full deny for PostgREST.
ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;

-- Verification queries (run separately after apply):
--   SELECT count(*) FROM submissions;
--   SELECT status, count(*) FROM submissions GROUP BY status;
--   SELECT funder_id, count(*) FROM submissions
--     WHERE submitted_at > NOW() - INTERVAL '30 days'
--     GROUP BY funder_id ORDER BY count(*) DESC;
