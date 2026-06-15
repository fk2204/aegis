-- Migration 057 — funder_note_submissions table.
--
-- Tracks individual "Submit to Funder" button clicks made from the
-- dossier (POST /ui/merchants/{merchant_id}/submit-to-funder in
-- src/aegis/web/routers/merchants.py). One row per click captures the
-- plain-text Note that was posted to the Close Lead activity feed and
-- the top matched funder it was framed against; subsequent funder
-- responses (approve / decline / counter / offer terms) mutate the
-- row in place.
--
-- Distinct from the existing ``submissions`` table (migration 013):
-- ``submissions`` is the paper-trail for the per-funder CSV bundle the
-- ``merchant_submit_to_funders`` handler emits (one row per funder per
-- bundle, document_id NOT NULL, csv_doc_hash/csv_filename required).
-- This table is the activity-feed analogue — no CSV, no document tie,
-- one row per Close Note POST. The two surfaces feed different parts of
-- the dossier and never overlap.
--
-- Status lifecycle (enforced application-side in
--   aegis.funder_note_submissions.repository):
--     pending
--       → approved
--       → declined
--       → countered
-- A pending → pending update is a no-op; once a non-pending status is
-- set, ``responded_at`` is stamped NOW() in the same UPDATE so the
-- dossier history block can render "Submitted 6h ago, no reply yet" vs
-- "Approved 4h after submission".
--
-- Trigger pattern: the existing AEGIS convention is to leave updated_at
-- maintenance to the application (migrations 013, 003, 022, 008 all
-- ship ``updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`` with no auto-
-- update trigger; the in-memory + Supabase repos stamp it on every
-- write). This migration adds a dedicated ``set_updated_at`` function +
-- trigger because the operator's manual UPDATE in the SQL console
-- (used during the funder-response capture flow before the operator UI
-- ships) would otherwise leave updated_at frozen at insert time, which
-- breaks the "last touched" sort in the dossier history block.
-- search_path is pinned to match the migration 030 hardening pass.

BEGIN;

CREATE TABLE IF NOT EXISTS funder_note_submissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Submission components. ON DELETE RESTRICT — a posted-note record is
  -- an audit artifact and must outlive merchant or funder deletion.
  merchant_id UUID NOT NULL REFERENCES merchants(id) ON DELETE RESTRICT,
  funder_id   UUID NOT NULL REFERENCES funders(id)   ON DELETE RESTRICT,

  -- Submission act. ``submitted_by`` is added (vs. the operator spec
  -- which omitted it) to mirror ``submissions.submitted_by`` from
  -- migration 013 — the audit_log row already captures actor_email but
  -- a join on every dossier-history render is wasteful; an inline copy
  -- here keeps the read path single-table.
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  submitted_by TEXT NOT NULL,

  -- Lifecycle
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'declined', 'countered')),

  -- Offer snapshot — populated once the funder responds with terms.
  -- offer_factor / offer_holdback are numeric(6,4) to match the
  -- submissions table's proposed_factor / proposed_holdback shape.
  offer_amount   NUMERIC(14, 2) CHECK (offer_amount IS NULL
                                    OR offer_amount > 0),
  offer_factor   NUMERIC(6, 4)  CHECK (offer_factor IS NULL
                                    OR (offer_factor > 1
                                        AND offer_factor <= 2)),
  offer_holdback NUMERIC(6, 4)  CHECK (offer_holdback IS NULL
                                    OR (offer_holdback >= 0
                                        AND offer_holdback <= 1)),

  -- Plain-text body that was posted to Close. Kept in the row so a
  -- regulator question ("what did you tell the funder?") is answerable
  -- without round-tripping the Close API.
  funder_note TEXT,

  -- Operator follow-up
  responded_at TIMESTAMPTZ,
  notes        TEXT,

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS funder_note_submissions_merchant_id_idx
  ON funder_note_submissions (merchant_id, submitted_at DESC);

CREATE INDEX IF NOT EXISTS funder_note_submissions_status_idx
  ON funder_note_submissions (status);

-- Dedicated trigger function — keeps the migration self-contained. The
-- public.updated_at() function migration 030 hardens is a Supabase
-- default whose existence is dashboard-managed (not git-managed);
-- depending on it here would couple this table's correctness to a
-- function we don't ship.
CREATE OR REPLACE FUNCTION funder_note_submissions_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS funder_note_submissions_set_updated_at
  ON funder_note_submissions;
CREATE TRIGGER funder_note_submissions_set_updated_at
  BEFORE UPDATE ON funder_note_submissions
  FOR EACH ROW EXECUTE FUNCTION funder_note_submissions_set_updated_at();

-- Match migrations 011 / 013 — service_role bypasses RLS; anon /
-- authenticated are denied. No policies = full deny for PostgREST.
ALTER TABLE funder_note_submissions ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT count(*) FROM funder_note_submissions;
--   SELECT status, count(*) FROM funder_note_submissions GROUP BY status;
--   SELECT merchant_id, count(*) FROM funder_note_submissions
--     WHERE submitted_at > NOW() - INTERVAL '30 days'
--     GROUP BY merchant_id ORDER BY count(*) DESC;
