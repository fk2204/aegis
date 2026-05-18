-- Migration 017 — overrides table (master plan §9.4)
--
-- The operator-override flywheel. Every time a human overrides AEGIS's
-- recommendation, a row lands here pinned to the specific decision_id
-- being overridden. `outcome` populates later when the funder responds
-- or the deal funds — closes the loop for tuning thresholds.
--
-- Reason codes are categorical (CHECK constraint enforced) so the
-- quarterly confusion matrix per reason code (master plan §20) can
-- aggregate without freeform-string normalization.
--
-- factors_disputed: JSONB map of {factor_name: operator's weight} when
-- the operator says "the score weighted X too heavily/lightly." Sparse
-- — only the factors the operator disputed.
--
-- pattern_false_positive: array of detector names the operator believes
-- fired wrongly. Same shape as ScoreResult.detectors.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS overrides (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  deal_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  decision_id UUID NOT NULL REFERENCES decisions(id) ON DELETE RESTRICT,
  original_recommendation TEXT NOT NULL,
  operator_decision TEXT NOT NULL,
  reason_code TEXT NOT NULL
    CHECK (reason_code IN (
      'score_too_conservative',
      'score_too_aggressive',
      'funder_specific_fit',
      'merchant_context_external',
      'data_quality_concern',
      'pattern_false_positive',
      'pattern_false_negative',
      'gut'
    )),
  reason_detail TEXT,
  factors_disputed JSONB,
  pattern_false_positive TEXT[],
  operator_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  outcome TEXT
    CHECK (outcome IS NULL OR outcome IN (
      'funded',
      'declined_by_funder',
      'charged_off',
      'paid_in_full'
    )),
  outcome_recorded_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_overrides_deal ON overrides (deal_id);
CREATE INDEX IF NOT EXISTS idx_overrides_decision ON overrides (decision_id);
CREATE INDEX IF NOT EXISTS idx_overrides_reason ON overrides (reason_code);
CREATE INDEX IF NOT EXISTS idx_overrides_outcome ON overrides (outcome)
  WHERE outcome IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_overrides_created ON overrides (created_at DESC);

ALTER TABLE overrides ENABLE ROW LEVEL SECURITY;
