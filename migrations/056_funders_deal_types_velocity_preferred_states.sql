-- Migration 056 — funder fields the matcher needs but doesn't yet have.
--
-- Three columns, all matcher-consumed:
--
--   * deal_types_accepted   — which products the funder writes (MCA,
--     LOC, Term Loan, etc.). Matches the Close Opportunity ``Deal Type``
--     dropdown. Empty array = "any deal type" (legacy behaviour). The
--     matcher hard-fails when this is non-empty AND the deal's type
--     isn't in the list.
--
--   * funding_velocity_days — business days from clean submission to
--     decision. Drives a soft concern when paired with an ASAP-urgency
--     merchant (Close Urgency = "ASAP (24-48 hours)"). The matcher
--     fires the concern when ``funding_velocity_days > 2`` for an ASAP
--     merchant — slow funders aren't disqualified, but the operator
--     gets a heads-up to set expectations.
--
--   * preferred_states      — funder-side soft geographic preference
--     (empty = no preference). When non-empty AND the merchant's state
--     isn't in the list, the matcher emits a soft concern. Distinct from
--     ``excluded_states`` which hard-fails.
--
-- Idempotency: schema_migrations gates re-runs at the application
-- layer. ``IF NOT EXISTS`` on each column lets a partial re-application
-- recover; non-trivial DDL has the same safe-fail discipline as
-- migration 027.

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS deal_types_accepted   TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS funding_velocity_days INT
    CHECK (funding_velocity_days IS NULL OR funding_velocity_days >= 0),
  ADD COLUMN IF NOT EXISTS preferred_states      TEXT[] NOT NULL DEFAULT '{}';
