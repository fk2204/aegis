-- Migration 106 — merchant_outcomes (2026-07-01 FIX 2).
--
-- Pre-submission operator outcome capture. Records what the operator
-- reported about a deal from the dossier's "What did the funder say?"
-- CTA — before/without a structured funder_note_submission or
-- funder_replies row existing. Feeds the calibration engine's
-- accuracy-tracking loop alongside the anchored funder_replies rows
-- so decline signals from Close-side workflows (no submission
-- generated yet) still land in ground truth.
--
-- Why a new table:
--   * ``funder_replies`` requires an anchor (deal_id XOR
--     submission_id) + NOT NULL funder_id — invariant load-bearing
--     for migration 071's outcome-stamping flow. A merchant-scope
--     button click has neither anchor.
--   * ``deal_outcomes`` (migration 074) is scored-decision anchored
--     (NOT NULL decision_id FK). Merchants with no scored decision
--     yet can't land there.
--   * The operator-button write path (POST /ui/merchants/{id}/outcomes/
--     {type}) needs a home that satisfies both: no anchor beyond
--     ``merchant_id``, optional funder for the case where the operator
--     already knows which funder responded.
--
-- Calibration read path:
--   ``aegis.scoring_v2.calibration.compute_and_store`` reads BOTH
--   ``funder_replies`` (anchored, per-funder) AND ``merchant_outcomes``
--   (merchant-scope) so ground truth from either write path lands in
--   the same weekly snapshot.

CREATE TABLE IF NOT EXISTS public.merchant_outcomes (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id  UUID NOT NULL REFERENCES public.merchants(id) ON DELETE CASCADE,
  outcome      TEXT NOT NULL
               CHECK (outcome IN ('funded', 'declined', 'countered', 'withdrawn')),
  funder_id    UUID REFERENCES public.funders(id) ON DELETE SET NULL,
  source       TEXT NOT NULL DEFAULT 'operator_button'
               CHECK (source IN ('operator_button', 'close_sync', 'backfill')),
  notes        TEXT,
  recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  recorded_by  TEXT
);

CREATE INDEX IF NOT EXISTS merchant_outcomes_merchant_idx
  ON public.merchant_outcomes(merchant_id);
CREATE INDEX IF NOT EXISTS merchant_outcomes_recorded_at_idx
  ON public.merchant_outcomes(recorded_at DESC);

ALTER TABLE public.merchant_outcomes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS deny_all_anon ON public.merchant_outcomes;
CREATE POLICY deny_all_anon ON public.merchant_outcomes
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

-- Verification (run separately):
--
--   SELECT column_name, data_type
--   FROM information_schema.columns
--   WHERE table_schema='public' AND table_name='merchant_outcomes';
--
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid='public.merchant_outcomes'::regclass;
