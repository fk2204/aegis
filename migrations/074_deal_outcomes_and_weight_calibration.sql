-- Migration 074 — deal_outcomes + weight_calibration_log (outcome feedback loop).
--
-- AEGIS today writes immutable decisions (migration 015 + 070) but has no
-- way to record what HAPPENED after the deal closed. This migration adds
-- two tables that close the loop:
--
--   1. ``deal_outcomes`` — one row per recorded post-fund outcome (paying,
--      paid_in_full, charged_off, defaulted, renewed, pending). MUTABLE
--      by design: outcome flips from ``pending`` → ``paying`` → terminal
--      over the life of the deal. Every UPDATE writes a paired audit_log
--      row via the application's existing audit-write discipline (see
--      ``src/aegis/scoring/weight_calibration.py`` + the
--      ``/ui/merchants/.../outcome`` route). No DB trigger needed — the
--      mutable-with-audit pattern in this codebase is Python-side
--      (mirrors how ``submissions`` / ``funder_replies`` handle updates).
--
--      Distinct from ``funder_replies`` (migration 071): that table
--      captures pre-fund outcomes (terms came back / declined). This
--      table captures post-fund outcomes (the deal funded; what
--      happened to it). Sibling tables, not overlapping.
--
--   2. ``weight_calibration_log`` — one row per operator review of a
--      weight-drift suggestion surfaced at ``/ui/calibration``. The
--      calibration report (``compute_weight_drift``) produces a per-
--      ``FRAUD_WEIGHTS`` key suggested weight from the empirical
--      charge-off ratio observed in ``deal_outcomes``. The operator
--      reviews each suggestion and records accepted / rejected /
--      deferred. NO auto-tuning of ``FRAUD_WEIGHTS`` — that constant is
--      edited by hand after operator review of the full report, same
--      shadow-first discipline that ships every other scoring change
--      (CLAUDE.md "Decision-boundary changes — deliberate + shadow-
--      first").
--
-- Foreign keys:
--   * ``deal_outcomes.decision_id`` → ``decisions(id)``. Decisions are
--     immutable (migration 070); the FK is RESTRICT on DELETE to mirror
--     ``decisions.deal_id``'s posture. An outcome refers back to the
--     decision row whose factors are being graded.
--   * ``deal_outcomes.funder_id`` → ``funders(id)`` is NULLABLE so an
--     outcome can be recorded even if the matched funder is later
--     archived (funder churn outlives historic outcomes).
--   * ``deal_outcomes.merchant_id`` is denormalized (no FK enforcement)
--     so a merchant soft-delete does not orphan the outcome row — the
--     row is part of the regulator-defense story and must outlive the
--     merchant. Matches the posture of audit_log.
--
-- CHECK constraints:
--   * ``funder_decision`` ∈ {approved, declined, countered}.
--   * ``outcome`` ∈ {paying, paid_in_full, charged_off, defaulted,
--     renewed, pending}. The calibration engine reads
--     ``outcome IN ('charged_off', 'defaulted')`` as the negative end
--     of the empirical comparison and ``outcome = 'paid_in_full'`` as
--     a clean positive.
--
-- Indexes: per-merchant lookup + per-decision lookup are the two read
-- paths the dossier + calibration engine drive. The partial index on
-- ``outcome != 'pending'`` keeps the calibration scan cheap (pending
-- rows have no signal yet).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

BEGIN;

CREATE TABLE IF NOT EXISTS deal_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id UUID NOT NULL,
    decision_id UUID NOT NULL REFERENCES decisions(id) ON DELETE RESTRICT,
    submitted_at TIMESTAMPTZ NOT NULL,
    funder_id UUID REFERENCES funders(id) ON DELETE SET NULL,
    funder_decision TEXT NOT NULL
      CHECK (funder_decision IN ('approved', 'declined', 'countered')),
    funded_amount NUMERIC(12, 2),
    factor_rate NUMERIC(6, 4),
    term_days INT,
    first_payment_date DATE,
    outcome TEXT NOT NULL
      CHECK (outcome IN (
        'paying',
        'paid_in_full',
        'charged_off',
        'defaulted',
        'renewed',
        'pending'
      )),
    outcome_recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    charge_off_amount NUMERIC(12, 2),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS deal_outcomes_merchant_id_idx
  ON deal_outcomes (merchant_id);
CREATE INDEX IF NOT EXISTS deal_outcomes_decision_id_idx
  ON deal_outcomes (decision_id);
-- Partial index: the calibration engine only joins on non-pending rows.
-- Pending rows have no outcome signal yet and are filtered out before the
-- empirical ratio is computed.
CREATE INDEX IF NOT EXISTS deal_outcomes_outcome_idx
  ON deal_outcomes (outcome)
  WHERE outcome != 'pending';

-- Default-deny RLS: outcome data is internal-only. Service role bypasses;
-- anon / authenticated are denied. Mirrors migrations 011 / 015 / 016.
ALTER TABLE deal_outcomes ENABLE ROW LEVEL SECURITY;


CREATE TABLE IF NOT EXISTS weight_calibration_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flag_code TEXT NOT NULL,
    suggested_weight NUMERIC(6, 2) NOT NULL,
    current_weight NUMERIC(6, 2) NOT NULL,
    operator_decision TEXT NOT NULL
      CHECK (operator_decision IN ('accepted', 'rejected', 'deferred')),
    operator_notes TEXT,
    sample_size INT NOT NULL,
    confidence TEXT NOT NULL
      CHECK (confidence IN ('low', 'medium', 'high')),
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS weight_calibration_log_flag_code_idx
  ON weight_calibration_log (flag_code);
CREATE INDEX IF NOT EXISTS weight_calibration_log_reviewed_at_idx
  ON weight_calibration_log (reviewed_at DESC);

ALTER TABLE weight_calibration_log ENABLE ROW LEVEL SECURITY;

-- Audit row for the schema change itself. One row per migration, not
-- one-per-table — matches the migration 072 pattern.
INSERT INTO audit_log (actor, action, subject_type, subject_id, details)
VALUES (
  'migration_074',
  'outcome_feedback_loop.schema_added',
  'table',
  NULL,
  jsonb_build_object(
    'tables_added', jsonb_build_array(
      'deal_outcomes',
      'weight_calibration_log'
    ),
    'rls_enabled', true,
    'fk_decision_id_restrict_on_delete', true
  )
);

COMMIT;
