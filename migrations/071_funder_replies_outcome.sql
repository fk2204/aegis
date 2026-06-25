-- Migration 071 — funder_replies outcome capture columns.
--
-- Adds operator-recorded outcome columns alongside the existing
-- email-parse path. The original 021 table assumed every row came
-- from an inbound webhook or operator-pasted email body (status +
-- raw_text NOT NULL, status in {approved,declined,countered}). This
-- migration extends the table for the "Record outcome" surface on
-- the dossier — the operator manually captures what the funder said
-- (or didn't say) for a specific funder_note_submission.
--
-- Why a separate outcome column instead of reusing status:
--   * status came from a CHECK constraint that does NOT include
--     'no_response' (an outcome an email body cannot express — by
--     definition there's no email when the funder ghosted).
--   * Adding 'no_response' to status would break the math-reconcile
--     semantics in src/aegis/funders/replies.validate_reply, which
--     treats every status as "the funder replied".
--   * Portfolio analytics needs to distinguish:
--       outcome='no_response' (operator confirmed: funder didn't reply)
--       outcome IS NULL          (operator hasn't recorded yet — pending)
--     This is the load-bearing distinction the per-funder approval
--     panel uses to stop conflating "no reply yet" with "ghosted".
--
-- Schema change strategy:
--   * ADD COLUMN IF NOT EXISTS — idempotent re-apply.
--   * status NOT NULL relaxed so manual no_response rows can omit it
--     (an outcome=no_response row has no funder-reply status to record).
--     Existing rows keep their non-NULL status; only future no_response
--     rows will have status NULL.
--   * raw_text NOT NULL relaxed for the same reason — a no_response
--     outcome has no email body to preserve.
--   * Existence-or-outcome CHECK guarantees every row carries at
--     least one of: a parsed reply (status+raw_text) or a recorded
--     outcome. Prevents empty placeholder rows.
--
-- Money / rate columns follow the project convention:
--   numeric(14,2) for amounts, numeric(6,4) for factor rates. Term
--   stored as days (int) — matches submissions.proposed_term_days.

BEGIN;

ALTER TABLE funder_replies
  ADD COLUMN IF NOT EXISTS outcome TEXT,
  ADD COLUMN IF NOT EXISTS outcome_amount NUMERIC(14, 2),
  ADD COLUMN IF NOT EXISTS outcome_factor_rate NUMERIC(6, 4),
  ADD COLUMN IF NOT EXISTS outcome_term_days INT,
  ADD COLUMN IF NOT EXISTS outcome_notes TEXT,
  ADD COLUMN IF NOT EXISTS outcome_recorded_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS outcome_recorded_by TEXT;

-- Relax status + raw_text NOT NULL to accommodate manual no_response
-- captures. The existence-or-outcome CHECK below guarantees one or the
-- other branch is populated; we never end up with both NULL.
ALTER TABLE funder_replies
  ALTER COLUMN status DROP NOT NULL,
  ALTER COLUMN raw_text DROP NOT NULL;

-- New outcome enum. Distinct from the existing status CHECK so the
-- email-parse path (status in {approved,declined,countered}) does
-- NOT have to grow a 'no_response' branch it cannot generate.
ALTER TABLE funder_replies
  DROP CONSTRAINT IF EXISTS funder_replies_outcome_check;
ALTER TABLE funder_replies
  ADD CONSTRAINT funder_replies_outcome_check
  CHECK (outcome IS NULL OR outcome IN
    ('approved', 'declined', 'countered', 'no_response'));

-- For declined / no_response outcomes the amount / factor / term make no
-- sense; enforce the invariant in the schema so a buggy writer can't
-- silently set them. approved / countered may set the offer fields.
ALTER TABLE funder_replies
  DROP CONSTRAINT IF EXISTS funder_replies_outcome_terms_check;
ALTER TABLE funder_replies
  ADD CONSTRAINT funder_replies_outcome_terms_check
  CHECK (
    outcome IS NULL
    OR outcome IN ('approved', 'countered')
    OR (
      outcome IN ('declined', 'no_response')
      AND outcome_amount IS NULL
      AND outcome_factor_rate IS NULL
      AND outcome_term_days IS NULL
    )
  );

-- Recording metadata: outcome and outcome_recorded_at + outcome_recorded_by
-- travel together. Either all three are populated, or none of them are.
ALTER TABLE funder_replies
  DROP CONSTRAINT IF EXISTS funder_replies_outcome_recorded_check;
ALTER TABLE funder_replies
  ADD CONSTRAINT funder_replies_outcome_recorded_check
  CHECK (
    (outcome IS NULL AND outcome_recorded_at IS NULL AND outcome_recorded_by IS NULL)
    OR (outcome IS NOT NULL AND outcome_recorded_at IS NOT NULL AND outcome_recorded_by IS NOT NULL)
  );

-- Existence invariant: every row carries either a parsed reply
-- (status + raw_text) OR a recorded outcome. No empty placeholders.
ALTER TABLE funder_replies
  DROP CONSTRAINT IF EXISTS funder_replies_reply_or_outcome_check;
ALTER TABLE funder_replies
  ADD CONSTRAINT funder_replies_reply_or_outcome_check
  CHECK (
    (status IS NOT NULL AND raw_text IS NOT NULL)
    OR outcome IS NOT NULL
  );

-- Index supports the portfolio per-funder outcome aggregation:
-- "approved/declined/countered/no_response counts grouped by funder
-- in a date window". Partial index on outcome IS NOT NULL keeps it
-- small (pending rows are excluded — they're not part of any aggregate).
CREATE INDEX IF NOT EXISTS idx_funder_replies_outcome_funder
  ON funder_replies (funder_id, outcome, outcome_recorded_at DESC)
  WHERE outcome IS NOT NULL;

COMMIT;
