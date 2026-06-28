-- Migration 086 — additive merchant columns for the enhanced UCC flow.
--
-- ADDITIVE ONLY. Migration 068 already created ``ucc_filings``,
-- ``ucc_default_indicators``, and ``ucc_checked_at`` on the merchants
-- table; those are NOT recreated here. The new columns capture:
--
--   * ``ucc_portal_url``       — verbatim state SOS / UCC search URL
--     rendered into the dossier so the operator has a single-click
--     verification path. Populated at write time from
--     ``aegis.business_intel.ucc_checker.UCC_STATE_PORTALS`` based on
--     ``merchants.state``.
--   * ``ucc_operator_verified``— True after the operator clicks the
--     "Mark UCC verified" button on the dossier and confirms (via the
--     existing override pattern) that the portal returned the expected
--     filings. Defaults to False so existing rows surface the
--     unverified state by default.
--   * ``ucc_verified_at``      — UTC timestamp of the verification
--     click. NULL on legacy + unverified rows.
--
-- Idempotent: ``ADD COLUMN IF NOT EXISTS`` so a partial re-apply or
-- bootstrap-probe-then-replay leaves the schema in the same end state.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS ucc_portal_url text,
  ADD COLUMN IF NOT EXISTS ucc_operator_verified boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS ucc_verified_at timestamptz;

COMMENT ON COLUMN merchants.ucc_portal_url IS
  'State SOS / UCC search URL rendered on the dossier verify-UCC link. '
  'Set from UCC_STATE_PORTALS[state] at merchant upsert time.';
COMMENT ON COLUMN merchants.ucc_operator_verified IS
  'True after operator confirms UCC findings via the dossier verify button.';
COMMENT ON COLUMN merchants.ucc_verified_at IS
  'UTC timestamp of the operator verification click. NULL when unverified.';

COMMIT;
