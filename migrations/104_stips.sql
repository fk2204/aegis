-- Migration 104 — stipulations tracking (2026-07-01 P3).
--
-- A "stip" (stipulation) is a document, verification, condition, or
-- signature the funder / underwriter requires before funding can
-- proceed. Every deal accumulates a small list; the operator marks
-- each as received / waived / expired as the merchant provides
-- (or fails to provide) them.
--
-- No AEGIS system currently tracks stips — the operator kept them
-- in a spreadsheet or in Close notes. Migration 104 gives them a
-- first-class table so the dossier can render "3 outstanding stips
-- for this deal" prominently and the calibration engine can measure
-- how many deals stall on stips.
--
-- Schema rationale:
--   * ``stip_type`` — the 4 categories the operator tracks:
--     'document' (bank statements, tax returns), 'verification'
--     (SOS check, OFAC), 'condition' (COJ, landlord letter), or
--     'signature' (ISO agreement, personal guarantee).
--   * ``status`` — 'outstanding' (default) → 'received' | 'waived' |
--     'expired'. No CHECK covers all transition rules; the app layer
--     enforces "outstanding -> {received, waived}" and "any -> expired"
--     via the PATCH route.
--   * ``waived_reason`` — required by the app when status flips to
--     'waived' so the audit trail explains why. Not a NOT NULL
--     constraint (the DB has no way to correlate status + reason).
--   * ``created_by`` — operator UUID (from the audit-log actor
--     surface); nullable to keep migration-side backfills unblocked.
--
-- RLS: enable + deny_all_anon per the migration-101 posture.

CREATE TABLE IF NOT EXISTS public.stips (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id   UUID NOT NULL REFERENCES public.merchants(id) ON DELETE CASCADE,
  funder_id     UUID REFERENCES public.funders(id) ON DELETE SET NULL,
  stip_type     TEXT NOT NULL
                CHECK (stip_type IN ('document', 'verification', 'condition', 'signature')),
  description   TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'outstanding'
                CHECK (status IN ('outstanding', 'received', 'waived', 'expired')),
  due_date      DATE,
  received_at   TIMESTAMPTZ,
  waived_reason TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by    UUID,
  notes         TEXT
);

-- Per-merchant lookup is the primary access pattern (dossier open).
CREATE INDEX IF NOT EXISTS stips_merchant_idx ON public.stips(merchant_id);

-- Outstanding-only partial index for the "N outstanding stips" badge
-- and the dashboard's "which deals are stuck on stips" query.
CREATE INDEX IF NOT EXISTS stips_outstanding_idx
  ON public.stips(merchant_id, status)
  WHERE status = 'outstanding';

-- RLS (migration-101 pattern).
ALTER TABLE public.stips ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS deny_all_anon ON public.stips;
CREATE POLICY deny_all_anon ON public.stips
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

-- Verification (run separately):
--
--   SELECT column_name, data_type
--   FROM information_schema.columns
--   WHERE table_schema='public' AND table_name='stips';
--
--   SELECT tablename, rowsecurity FROM pg_tables
--   WHERE schemaname='public' AND tablename='stips';
