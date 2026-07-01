-- Migration 105 — analyses.business_risk_band (2026-07-01 GAP 2).
--
-- Track B's ``BusinessRiskBand`` (BandLevel: low / moderate / elevated /
-- high) is currently computed at every dossier render but never
-- persisted. Downstream consumers — calibration engine, dashboard
-- filters, funder-match band-aware routing — have to rebuild the
-- score every time they need the band. Persisting on the ``analyses``
-- row means the dashboard's Ready-to-Submit filter, the funder-
-- matching pre-fetch job (2026-07-01 GAP 1), and the calibration
-- weekly cron all consume the same durable value the operator saw
-- when they opened the dossier.
--
-- Column shape:
--   * TEXT with a CHECK constraint pinning the four ``BandLevel``
--     literals. Matches the existing ``track_a_verdict`` posture on
--     the same table (also TEXT + CHECK enum).
--   * NULL by default — legacy rows written before this migration
--     stay NULL until a dossier render or the calibration cron
--     back-writes them.
--
-- Write path: ``aegis.storage.persist_business_risk_band`` (added in
-- the same commit) fires as a single UPDATE from the dossier route
-- once Track B compute lands the band. Idempotent — repeated writes
-- with the same value are no-ops at the row-diff layer.

ALTER TABLE analyses
  ADD COLUMN IF NOT EXISTS business_risk_band TEXT;

ALTER TABLE analyses
  DROP CONSTRAINT IF EXISTS analyses_business_risk_band_check;

ALTER TABLE analyses
  ADD CONSTRAINT analyses_business_risk_band_check
    CHECK (
      business_risk_band IS NULL
      OR business_risk_band IN ('low', 'moderate', 'elevated', 'high')
    );

-- Verification (run separately):
--
--   SELECT column_name, data_type
--   FROM information_schema.columns
--   WHERE table_schema='public' AND table_name='analyses'
--     AND column_name='business_risk_band';
--
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid='public.analyses'::regclass
--     AND conname='analyses_business_risk_band_check';
