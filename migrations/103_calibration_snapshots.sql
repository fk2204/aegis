-- Migration 103 — calibration_snapshots: weekly accuracy measurements.
--
-- The 2026-06-30 audit (P1/B3) established that AEGIS has zero
-- recorded outcomes (decisions: 62 manual_review snapshots only;
-- funder_replies: 0 rows). The B2 commit (4c8b85f) added the
-- prominent dossier outcome capture flow; this migration is the
-- table the calibration engine writes weekly snapshots into.
--
-- Schema rationale:
--
--   * computed_at                  - snapshot timestamp (UTC).
--   * outcome_count                - number of outcomes contributing
--                                    to this snapshot. Calibration is
--                                    skipped when < MIN_OUTCOMES (20).
--   * fraud_true_positive_rate     - of deals AEGIS flagged as fraud
--                                    that turned out to actually fail
--                                    (declined / charged off / defaulted).
--   * fraud_false_positive_rate    - of deals AEGIS flagged that the
--                                    funder funded anyway.
--   * revenue_mean_abs_error       - mean absolute error between
--                                    AEGIS's true_revenue and the
--                                    funder's underwritten revenue
--                                    (when captured).
--   * paper_grade_accuracy         - share of AEGIS paper grades that
--                                    matched the funder's grade.
--   * top_false_positive_signals   - JSONB list of the 5 most-fired
--                                    signals on deals that funded
--                                    anyway. Calibration steers
--                                    threshold review priority.
--   * top_missed_signals           - JSONB list of the 5 signals that
--                                    DID NOT fire on declined deals
--                                    (the false-negatives surface).
--   * raw_metrics                  - JSONB escape hatch for any
--                                    secondary metric the engine
--                                    surfaces. Forward-compat.
--
-- RLS posture: service_role bypasses (migration 011); explicit
-- deny_all_anon mirrors the migration-101 pattern. The portfolio
-- page reads via the backend only.

CREATE TABLE IF NOT EXISTS public.calibration_snapshots (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  computed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  outcome_count               INTEGER NOT NULL,
  fraud_true_positive_rate    NUMERIC(5, 4),
  fraud_false_positive_rate   NUMERIC(5, 4),
  revenue_mean_abs_error      NUMERIC(14, 2),
  paper_grade_accuracy        NUMERIC(5, 4),
  top_false_positive_signals  JSONB,
  top_missed_signals          JSONB,
  raw_metrics                 JSONB
);

-- Index for "give me the most recent N snapshots" — the portfolio
-- page query.
CREATE INDEX IF NOT EXISTS calibration_snapshots_computed_at_idx
  ON public.calibration_snapshots(computed_at DESC);

-- RLS posture mirrors migration 101 — explicit deny on anon /
-- authenticated; service_role bypasses by default.
ALTER TABLE public.calibration_snapshots ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS deny_all_anon ON public.calibration_snapshots;
CREATE POLICY deny_all_anon ON public.calibration_snapshots
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

-- Verification (run separately):
--
--   SELECT column_name, data_type
--   FROM information_schema.columns
--   WHERE table_schema='public' AND table_name='calibration_snapshots';
--
--   SELECT tablename, rowsecurity FROM pg_tables
--   WHERE schemaname='public' AND tablename='calibration_snapshots';
