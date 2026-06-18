-- 067_merchant_web_presence.sql
--
-- Adds three nullable columns to ``merchants`` for the reputation-scan
-- feature (see ``aegis.web_presence.scanner``):
--
--   * ``web_presence_summary``     — 2-sentence reputation paragraph
--     produced by Claude using the web_search tool. Operator-readable;
--     never used as a hard gate.
--   * ``web_presence_flags``       — string-array of short red-flag
--     tags surfaced by the scan (e.g. ``bbb_unresolved_complaints``,
--     ``permanently_closed``). When non-empty, ``match_funder``
--     converts each tag into a ``FunderMatch.soft_concerns`` entry.
--   * ``web_presence_scanned_at``  — UTC timestamp of the most-recent
--     scan. ``NULL`` triggers the scorer's "scan on first score" path.
--
-- Reputation scans are soft signals, never decline reasons. All three
-- columns are nullable so existing merchants stay untouched until the
-- next score / refresh.

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS web_presence_summary text NULL,
  ADD COLUMN IF NOT EXISTS web_presence_flags text[] NULL,
  ADD COLUMN IF NOT EXISTS web_presence_scanned_at timestamptz NULL;

COMMENT ON COLUMN merchants.web_presence_summary IS
  '2-sentence reputation summary from web_presence scanner. Soft signal only.';
COMMENT ON COLUMN merchants.web_presence_flags IS
  'Lowercase-snake_case red-flag tags from the scanner; surfaced as match soft_concerns.';
COMMENT ON COLUMN merchants.web_presence_scanned_at IS
  'UTC timestamp of the last scan. NULL = needs first scan when next scored.';
