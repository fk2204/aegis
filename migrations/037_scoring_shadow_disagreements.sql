-- Migration 037 — scoring_shadow_disagreements (R1.6 Step 2 cutover prep).
--
-- Background
-- ----------
-- Commit 973d7fd shipped scripts/shadow_comparison_a_b_c_vs_fraud_score.py,
-- a corpus-walking read-only diagnostic that compares the LIVE
-- ``score_deal`` pipeline (legacy ``fraud_score`` hard-decline path) to
-- the new Track A / B / C tracks for every merchant. The script
-- categorises each disagreement into one of five buckets:
--
--   * agreement                          — both surfaces agree
--   * new-is-better                      — new tracks catch what live missed
--   * old-caught-something-new-misses    — REGRESSION sentinel (loud)
--   * genuinely-ambiguous                — operator judgment needed
--   * insufficient-new-data              — neither surface actionable
--
-- The audit gates the Step 2 fraud_score→A/B/C cutover on weeks of
-- ongoing review: corpus growth, Track A historical lookback, and
-- per-disagreement triage. Today the script prints to stdout; nothing
-- persists. This migration creates the durable triage queue: one row
-- per (merchant_id × comparison_run × evidence-shape) disagreement,
-- with operator-side fields for who triaged it and what they decided.
--
-- Soft FK to merchants
-- --------------------
-- ``merchant_id`` is NOT FK-enforced. The corpus comparison run may
-- include synthetic merchant ids (generated PDF fixtures whose merchant
-- rows live only in a test DB, not in the row this audit lives in).
-- The triage queue must be able to record those without violating a
-- foreign-key constraint. ``deal_id`` is similarly soft (and nullable —
-- many merchants in shadow comparison have no decisioned deal yet).
--
-- evidence JSONB shape
-- --------------------
-- The script's _categorise() function uses the following inputs to
-- assign a category:
--
--   live_hard_reasons    — list[str] from ScoreResult.hard_decline_reasons
--   live_soft_concerns   — list[str] from ScoreResult.soft_concerns
--   new_integrity        — 'clean' | 'review' | 'fail' | None
--   new_band             — 'low' | 'moderate' | 'elevated' | 'high' | None
--   new_band_factors     — list[str] (e.g. 'international_concentration')
--   new_intl_share_pct   — float | None
--
-- ``evidence`` JSONB pins ALL of those at insert time so a future
-- triage operator can see what drove the categorisation without
-- re-running the comparison. It must be free of PII (no merchant names,
-- no transaction descriptions, no account numbers) — the columns and
-- evidence keys are intentionally categorical/numeric.
--
-- Indexes
-- -------
--   * (category, comparison_run_at DESC) — regression sentinel queue:
--     "show me every old-caught-something-new-misses row newest first"
--   * (triaged_at) — open vs closed; the view scoring_disagreements_open
--     (migration 038) filters on ``triaged_at IS NULL``
--   * (merchant_id, comparison_run_at DESC) — per-merchant history:
--     "show me every disagreement we've ever recorded for this merchant"

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS scoring_shadow_disagreements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Soft FK to merchants — see header note. Corpus runs include
  -- synthetic merchant ids so a hard FK would break re-use of the
  -- comparison script against fixtures.
  merchant_id UUID NOT NULL,

  -- Optional deal context — many merchants in shadow comparison have
  -- no decisioned deal yet, so nullable.
  deal_id UUID,

  -- Timestamp of the comparison run that produced this row. Used by
  -- the (category, comparison_run_at DESC) index for the regression
  -- queue and by the open-view ordering.
  comparison_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- LIVE side (legacy fraud_score path). Nullable so a row can record
  -- "live could not score" (merchant not finalized, no analyzed docs).
  legacy_fraud_score INTEGER,
  legacy_tier VARCHAR(4),
  legacy_recommendation VARCHAR(32),
  legacy_hard_declines JSONB,

  -- NEW side (Track A / B / C). Nullable for the same reason — the
  -- new view may report ``insufficient_data_reason`` instead of a
  -- verdict on small corpora.
  track_a_verdict VARCHAR(16),
  track_b_band VARCHAR(16),
  track_c_panel JSONB,

  -- One of:
  --   'agreement'
  --   'new-is-better'
  --   'old-caught-something-new-misses'
  --   'genuinely-ambiguous'
  --   'insufficient-new-data'
  category VARCHAR(48) NOT NULL,

  -- Per-track reasons that drove _categorise(). Self-contained:
  -- future operator review must be able to read the row without
  -- re-running the script. PII-free by construction (categorical
  -- enums + numeric shares only). Schema:
  --   {
  --     "rationale": str,
  --     "live_hard_reasons": list[str],
  --     "live_soft_concerns": list[str],
  --     "track_a_branches": list[str],
  --     "track_b_factors": list[str],
  --     "track_b_action": str | null,
  --     "intl_share_pct": float | null,
  --     "live_error": str | null,
  --     "new_error": str | null
  --   }
  evidence JSONB,

  -- Operator triage fields. Nullable until the operator reviews.
  triaged_by VARCHAR(128),
  triaged_at TIMESTAMPTZ,

  -- One of (when triaged):
  --   'accept-new'         — Track A/B/C verdict wins, future cutover safe
  --   'accept-old'         — legacy fraud_score path wins; do NOT cut over
  --   'both-valid'         — semantic disagreement, neither is wrong
  --   'needs-rule-change'  — a track/severity needs adjusting before cutover
  triage_decision VARCHAR(32),
  triage_notes TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Regression sentinel queue: ``category='old-caught-something-new-misses'``
-- newest first. Drives the operator-facing alert.
CREATE INDEX IF NOT EXISTS idx_scoring_shadow_disagreements_category_run
  ON scoring_shadow_disagreements (category, comparison_run_at DESC);

-- Open-vs-closed scan: the view scoring_disagreements_open filters
-- ``triaged_at IS NULL``; this index makes that filter cheap.
CREATE INDEX IF NOT EXISTS idx_scoring_shadow_disagreements_triaged_at
  ON scoring_shadow_disagreements (triaged_at);

-- Per-merchant history: "show me every disagreement we've ever recorded
-- for this merchant" newest first.
CREATE INDEX IF NOT EXISTS idx_scoring_shadow_disagreements_merchant_run
  ON scoring_shadow_disagreements (merchant_id, comparison_run_at DESC);

-- Default-deny RLS: this is internal-only audit data, accessible to
-- the service role only (the comparison script and operator triage UI).
-- Mirrors the pattern from migrations 016 (disclosures) and 036
-- (disclosure_transmissions).
ALTER TABLE scoring_shadow_disagreements ENABLE ROW LEVEL SECURITY;
