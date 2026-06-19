-- 068_merchant_ucc.sql
--
-- Adds three nullable columns to ``merchants`` for the UCC + previous-
-- default lookup (see ``aegis.business_intel.ucc_checker``):
--
--   * ``ucc_filings``           — list of secured-party strings the
--     scanner found in public UCC filings against the business.
--   * ``ucc_default_indicators``— short red-flag strings the scanner
--     surfaced from lawsuits / judgments / MCA-default news mentions.
--   * ``ucc_checked_at``        — UTC timestamp of the last check.
--     ``NULL`` triggers the scorer's "check on first score" path.
--
-- Both lists collapse to ``FunderMatch.soft_concerns`` via match_funder
-- so every UCC finding shows next to the underwriter's other concerns.

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS ucc_filings text[] NULL,
  ADD COLUMN IF NOT EXISTS ucc_default_indicators text[] NULL,
  ADD COLUMN IF NOT EXISTS ucc_checked_at timestamptz NULL;

COMMENT ON COLUMN merchants.ucc_filings IS
  'Secured-party strings from public UCC filings; surfaced as soft_concerns via match_funder.';
COMMENT ON COLUMN merchants.ucc_default_indicators IS
  'Lawsuit / judgment / default flag strings from the UCC checker.';
COMMENT ON COLUMN merchants.ucc_checked_at IS
  'UTC timestamp of the last UCC + default check. NULL = needs first check on next score.';
