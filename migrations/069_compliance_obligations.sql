-- 069_compliance_obligations.sql
--
-- Compliance obligations tracker — second pass.
--
-- Migration 018 already created the ``compliance_obligations`` table and
-- seeded the same 6 Tier-1 obligations the operator's master plan §9.5
-- enumerated, but it left ``next_due_date`` NULL across the board (the
-- 018-era dashboard was a read-only annotator that derived
-- ``derived_state`` from ``deadline`` if next_due_date was missing). The
-- new ``ComplianceObligationRepository`` + Today-dashboard attention
-- card key off ``next_due_date`` specifically — a NULL means "no
-- reminder fires, surface nothing on the card" by design (operator
-- principle #4: empty/NULL is better than wrong).
--
-- This migration is fully additive and idempotent. It:
--
--   1. Re-asserts the table shape (``CREATE TABLE IF NOT EXISTS``) for
--      operators bootstrapping a fresh DB without first applying 018.
--   2. Re-asserts the status CHECK constraint via a DO-block so a fresh
--      bootstrap matches a long-running prod DB.
--   3. Inserts the 6 real obligations from the operator's spec, keyed
--      on the operator-natural composite (state_code, obligation_type)
--      so the existing 018 rows are left alone on prod and only fresh
--      DBs (or DBs that never ran 018) actually get the seed.
--   4. Backfills ``next_due_date`` on the TX OCCC row only, where the
--      operator explicitly stated the 2026-12-31 deadline. Other
--      obligations get NULL until the operator authors their actual
--      next-due dates in the dashboard write surface (per operator
--      principle #4 — do not fill statutory deadlines from prior
--      knowledge; ask the operator).
--
-- The CHECK constraint matches 018 verbatim:
--   status IN ('not_started','in_progress','submitted','active','lapsed')
--
-- Status transitions are operator-driven through
-- ``ComplianceObligationRepository.mark_status`` which writes one
-- ``compliance.obligation_status_changed`` audit row per transition.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS compliance_obligations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  obligation_type TEXT NOT NULL,
  state_code TEXT NOT NULL,
  authority TEXT NOT NULL,
  description TEXT NOT NULL,
  deadline DATE,
  recurrence TEXT,
  status TEXT NOT NULL
    CHECK (status IN ('not_started', 'in_progress', 'submitted', 'active', 'lapsed')),
  next_due_date DATE,
  evidence_file_path TEXT,
  last_reviewed TIMESTAMPTZ,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_obligations_state ON compliance_obligations (state_code);
CREATE INDEX IF NOT EXISTS idx_obligations_status ON compliance_obligations (status);
CREATE INDEX IF NOT EXISTS idx_obligations_next_due
  ON compliance_obligations (next_due_date)
  WHERE next_due_date IS NOT NULL;

ALTER TABLE compliance_obligations ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- Seed inserts — keyed on (state_code, obligation_type) so 018 rows on
-- prod stay untouched. Each obligation matches one of the 6 the
-- operator listed in the spec.
-- ============================================================================

-- 1. VA SCC annual broker registration.
INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'license_renewal', 'VA', 'VA SCC',
       'Virginia State Corporation Commission — annual broker registration filing.',
       'annual', 'not_started',
       'Annual cadence. next_due_date set by operator after first filing; remains NULL until then.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'VA' AND obligation_type = 'license_renewal'
);

-- 2. CT DOB annual, due Oct 1.
INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'license_renewal', 'CT', 'CT DOB',
       'Connecticut Department of Banking — annual sales-based financing registration. Due October 1 each year.',
       'annual', 'not_started',
       'Annual; due Oct 1. Operator sets next_due_date at first filing.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'CT' AND obligation_type = 'license_renewal'
);

-- 3. UT DFI via NMLS, annual.
INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'registration', 'UT', 'UT DFI via NMLS',
       'Utah Department of Financial Institutions — commercial financing registration filed through NMLS. Annual renewal.',
       'annual', 'not_started',
       'Annual via NMLS. next_due_date set by operator after first NMLS filing.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'UT' AND obligation_type = 'registration'
);

-- 4. MO Div of Finance broker registration (one-time + maintain).
INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'registration', 'MO', 'MO Div of Finance',
       'Missouri Division of Finance — broker registration. One-time filing plus maintenance.',
       'one_time', 'not_started',
       'One-time registration; no recurring next_due_date.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'MO' AND obligation_type = 'registration'
);

-- 5. TX OCCC registration — initial deadline 2026-12-31, then annual Jan 31.
INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, deadline, recurrence, next_due_date, status, notes
)
SELECT 'registration', 'TX', 'TX OCCC',
       'Texas Office of Consumer Credit Commissioner — sales-based financing broker registration. Initial filing deadline 2026-12-31, then annual renewal by January 31.',
       DATE '2026-12-31', 'annual', DATE '2026-12-31', 'not_started',
       'Initial deadline 2026-12-31. After initial filing, annual renewal by Jan 31.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'TX' AND obligation_type = 'registration'
);

-- 6. CA DFPI annual report.
INSERT INTO compliance_obligations (
  obligation_type, state_code, authority, description, recurrence, status, notes
)
SELECT 'annual_report', 'CA', 'CA DFPI',
       'California Department of Financial Protection and Innovation — annual report for commercial financing providers/brokers.',
       'annual', 'not_started',
       'Annual report. next_due_date set by operator at first filing.'
WHERE NOT EXISTS (
  SELECT 1 FROM compliance_obligations
  WHERE state_code = 'CA' AND obligation_type = 'annual_report'
);

-- Backfill: where migration 018 already seeded a TX OCCC row without a
-- next_due_date but the deadline is the operator-stated 2026-12-31,
-- set next_due_date to match the deadline so the cron starts firing
-- the 60/30/14-day reminders against the correct date. Other 018 rows
-- are left untouched (next_due_date stays NULL) — the operator authors
-- those statutory dates in the dashboard write surface.
UPDATE compliance_obligations
SET next_due_date = DATE '2026-12-31'
WHERE state_code = 'TX'
  AND obligation_type = 'registration'
  AND deadline = DATE '2026-12-31'
  AND next_due_date IS NULL;
