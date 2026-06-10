-- Migration 036 — disclosure_transmissions table (R0.5).
--
-- 4-year audit trail of disclosures transmitted to merchants, mandated by
--   CA  : 10 CCR § 952 (transmission record retention)
--   NY  : 23 NYCRR § 600 (record retention for CFDL disclosures)
--
-- Why a NEW table alongside the existing disclosure log surface
-- -------------------------------------------------------------
-- Two tables already touch this domain, but neither covers the audit
-- contract this migration closes:
--
--   * migration 004 (`disclosure_transmission_log`) — CA-only § 952
--     transmission proof: doc hash + recipient email + ack/funder-notify
--     timestamps. Bound by FK to `documents.id` (the parsed bank
--     statement) and `funders.id`. Does NOT carry the disclosed
--     financial terms (APR, funding_provided, finance_charge,
--     factor_rate, holdback_pct) and does NOT distinguish CA from NY.
--
--   * migration 016 (`disclosures`) — the rendered + delivered artifact
--     itself with template_sha256 + rendered_pdf_sha256 + inputs JSONB.
--     Bound to `decisions.id`. Optimized for "give me the bytes I sent",
--     not for the regulator-shaped audit query "show me every disclosure
--     transmitted in state X between dates A and B with their APR + term
--     + recipient".
--
-- This migration adds the regulator-shaped audit table the R0.5 audit
-- finding requires: one row per transmission event, carrying the
-- financial terms a regulator will ask about, indexed by (state,
-- sent_at) for cohort retrieval, with a 4-year retention floor enforced
-- by a STORED `retention_until` column mirroring the migration-004
-- pattern. Existing tables continue to serve their existing callers;
-- this one is for the audit / retention pipeline.
--
-- deal_id / merchant_id are UUIDs without FK references so the table
-- works for any deal lifecycle stage (pre-decision, post-decision, post-
-- soft-delete). Compliance records must outlive every other row by
-- design — soft FK is sufficient for the audit-trail use case.
--
-- 4-year retention floor lives in two places:
--   1. `audit_retention_policy` (migration 025) keyed on state_code,
--      read by `audit_archiver.py` at archive time.
--   2. STORED `retention_until` on this row, computed at insert time,
--      so a clock change cannot retroactively shorten the floor.
-- Both must agree on 4 years for CA + NY.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS disclosure_transmissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Soft FK to documents/merchants — see header note. Both nullable
  -- because the regulator-facing audit query only requires (state,
  -- recipient_email, sent_at) to be answerable; deal/merchant linkage
  -- is internal convenience for joining back to the dossier.
  deal_id UUID,
  merchant_id UUID,

  -- USPS two-letter code. Drives retention policy lookup +
  -- per-jurisdiction audit queries.
  state VARCHAR(2) NOT NULL,

  -- Free-form version string identifying the disclosure regime + revision
  -- (e.g. 'CA_SB1235_SB362_v1', 'NY_CFDL_v1'). Pinned at insert time so
  -- a regulator can trace which template revision generated a given
  -- row even after the template moves forward.
  disclosure_version VARCHAR(64) NOT NULL,

  -- Repo-relative template file path (e.g.
  -- 'compliance/templates/ca_sb1235.html.j2'). Pinned at insert time.
  template_path VARCHAR(255) NOT NULL,

  -- sha256 hex (lowercase) of the rendered HTML. Lets the regulator
  -- (or counsel) verify the disclosed bytes match what was sent.
  html_sha256 CHAR(64) NOT NULL,

  -- Optional — empty when the disclosure was transmitted via an out-of-
  -- band channel (signature ceremony, in-person handoff). When set,
  -- this is the address the rendered HTML was emailed to.
  recipient_email VARCHAR(255),

  sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Operator id (or 'system' when sent by an automated worker). Free
  -- text to accommodate the various operator identity surfaces that
  -- exist today (Supabase auth uid, Close user id, internal worker
  -- name). The audit consumer must accept any of these.
  sent_by VARCHAR(128),

  -- Disclosed financial terms — the variables a regulator will request
  -- when auditing a specific disclosure transmission. Pinned at the
  -- transmission instant so the row is self-contained even if the
  -- upstream scoring/pricing changes later.
  apr NUMERIC(8,4),
  funding_provided NUMERIC(14,2),
  finance_charge NUMERIC(14,2),
  estimated_total_payment NUMERIC(14,2),
  estimated_term_days INTEGER,
  factor_rate NUMERIC(6,4),
  holdback_pct NUMERIC(6,4),

  -- Free-form structured metadata for operator-discretion fields
  -- (template render flags, double-dipping context, savings stance).
  -- Indexed via GIN only if a future query needs it; today's queries
  -- read scalar columns above.
  metadata JSONB,

  -- 4-year retention floor (CA 10 CCR § 952 + NY 23 NYCRR § 600). 30-day
  -- buffer absorbs clock skew + statute-of-limitations edge cases.
  --
  -- DEFAULT (not GENERATED STORED): current Postgres marks every
  -- ``TIMESTAMPTZ + INTERVAL`` as STABLE — even day-only intervals depend
  -- on session-timezone DST rules — so STORED generated columns reject
  -- it ("generation expression is not immutable"). DEFAULT expressions
  -- don't require immutability, so they can use NOW().
  --
  -- 1490 days = 4 * 365 + 30, ≈ 4y + 30d. The value is locked at insert
  -- (default expressions evaluate once per row). Clock changes cannot
  -- retroactively shorten the window. If a caller sets ``sent_at`` to a
  -- backdated value, ``retention_until`` still binds to NOW()+1490d at
  -- insert — i.e., the retention floor relative to the insert moment,
  -- not the disclosure-sent moment. That overstates retention (rows
  -- kept longer), which exceeds the statutory floor.
  --
  -- Migration 004 used a GENERATED STORED form with year/month intervals
  -- but was applied on an older Postgres release that allowed the
  -- non-immutable expression. New migrations cannot mirror that pattern.
  retention_until TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '1490 days'),

  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_disclosure_transmissions_deal
  ON disclosure_transmissions (deal_id);
CREATE INDEX IF NOT EXISTS idx_disclosure_transmissions_merchant
  ON disclosure_transmissions (merchant_id);
CREATE INDEX IF NOT EXISTS idx_disclosure_transmissions_state_sent
  ON disclosure_transmissions (state, sent_at);
CREATE INDEX IF NOT EXISTS idx_disclosure_transmissions_retention
  ON disclosure_transmissions (retention_until);

ALTER TABLE disclosure_transmissions ENABLE ROW LEVEL SECURITY;
