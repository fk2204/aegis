-- Migration 038 — scoring_disagreements_open view (R1.6 Step 2 cutover prep).
--
-- Background
-- ----------
-- Migration 037 created ``scoring_shadow_disagreements`` — the durable
-- triage queue produced by ``scripts/scoring_shadow_compare.py --persist``.
-- This view is the operator's open-triage queue: every disagreement row
-- whose ``triaged_at IS NULL``, ordered so the loudest categories
-- surface first.
--
-- Ordering rationale
-- ------------------
-- The Step 2 cutover (retire fraud_score; flip A/B/C live) is gated on
-- zero regressions in the live shadow. The
-- ``old-caught-something-new-misses`` category IS the regression
-- sentinel — those rows must surface first or the cutover gate is
-- meaningless. Ordering:
--
--   1. old-caught-something-new-misses   (REGRESSION sentinel — loud)
--   2. new-is-better                     (cutover-supporting evidence)
--   3. genuinely-ambiguous               (operator judgment needed)
--   4. agreement                         (lowest priority but kept for completeness)
--   5. insufficient-new-data             (informational only)
--
-- Within each category, newest ``comparison_run_at`` first so the freshest
-- evidence is at the top of the queue.
--
-- View, not materialized view
-- ---------------------------
-- The underlying table is small (one row per disagreement per run, RLS
-- service-role only, internal audit data). The triage queue is read on
-- operator demand, not at high frequency. A plain view keeps the
-- contract "always reads from the live table" without a refresh story.

CREATE OR REPLACE VIEW scoring_disagreements_open AS
SELECT
  d.id,
  d.merchant_id,
  d.deal_id,
  d.comparison_run_at,
  d.legacy_fraud_score,
  d.legacy_tier,
  d.legacy_recommendation,
  d.legacy_hard_declines,
  d.track_a_verdict,
  d.track_b_band,
  d.track_c_panel,
  d.category,
  d.evidence,
  d.created_at
FROM scoring_shadow_disagreements AS d
WHERE d.triaged_at IS NULL
ORDER BY
  CASE d.category
    WHEN 'old-caught-something-new-misses' THEN 0
    WHEN 'new-is-better'                    THEN 1
    WHEN 'genuinely-ambiguous'              THEN 2
    WHEN 'agreement'                        THEN 3
    WHEN 'insufficient-new-data'            THEN 4
    ELSE 99
  END,
  d.comparison_run_at DESC;
