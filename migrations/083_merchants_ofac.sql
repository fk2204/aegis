-- 083_merchants_ofac.sql
--
-- Adds four columns to ``merchants`` for OFAC SDN + Consolidated
-- sanctions screening (see ``aegis.compliance.ofac``):
--
--   * ``ofac_checked_at`` — UTC timestamp of the last screening run.
--     ``NULL`` triggers ``ensure_ofac_check`` on the next score.
--   * ``ofac_is_clear``   — boolean. ``False`` is a hard block: the
--     dossier renders a red banner and the funder-matching grid is
--     suppressed. ``True`` lets the existing scoring path proceed.
--   * ``ofac_match_detail`` — text[] of human-readable match strings
--     ("sdn:12345 :: SANC NAME (jw=0.92 ts=0.00)") when blocked.
--   * ``ofac_cache_date`` — UTC timestamp the screened cache was
--     fetched. Lets the dossier surface "screened against OFAC list
--     refreshed N hours ago" so an operator can decide whether to
--     re-run after a stale-cache outage.
--
-- All four columns nullable for forward-compat with pre-083 merchant
-- rows; the ``ensure_ofac_check`` lazy hook populates them on first
-- score after migration.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS ofac_checked_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS ofac_is_clear boolean NULL,
  ADD COLUMN IF NOT EXISTS ofac_match_detail text[] NULL,
  ADD COLUMN IF NOT EXISTS ofac_cache_date timestamptz NULL;

COMMENT ON COLUMN merchants.ofac_checked_at IS
  'UTC timestamp of last OFAC SDN screening; NULL triggers ensure_ofac_check.';
COMMENT ON COLUMN merchants.ofac_is_clear IS
  'False blocks funder-matching grid and renders a red dossier banner.';
COMMENT ON COLUMN merchants.ofac_match_detail IS
  'Human-readable match strings populated when ofac_is_clear=false.';
COMMENT ON COLUMN merchants.ofac_cache_date IS
  'UTC fetched_at of the cache file used for the screening run.';

COMMIT;
