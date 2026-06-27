-- Migration 079 — add hints_source provenance to bank_layouts.
--
-- Layout-learning hints arrive from two writers now:
--   * 'manual'  — operator-authored via /ui/bank-layouts or
--                 scripts/seed_bank_hints.py. Higher trust signal; needs
--                 HINTS_AVAILABLE_THRESHOLD (3) successful parses before
--                 it feeds the Bedrock extraction prompt.
--   * 'auto'    — derived deterministically from a single successful
--                 parse by ``src/aegis/bank_layouts/auto_hints.py``.
--                 Lower trust per-hint but high volume; needs
--                 AUTO_HINTS_AVAILABLE_THRESHOLD (1) to inject so a
--                 first-ever parse of a new bank can immediately
--                 benefit on the second parse.
--   * 'mixed'   — both writers have contributed. Conservative gate
--                 wins: threshold matches the 'manual' (3-parse) bar.
--
-- The threshold split is enforced in the repository's ``get_hints``
-- read path, not in the schema — adding a routing column would
-- require dual-write coordination on every hint update. The repository
-- promotes 'auto' → 'mixed' when a manual writer follows an auto
-- writer (or vice versa); the value here describes "what's currently
-- in extraction_hints", not "who wrote last".
--
-- Backfill: every existing bank_layouts row with non-null
-- extraction_hints was authored via the operator UI route or
-- seed_bank_hints.py — both manual sources. Rows with no hints get
-- the table default 'auto', which is harmless (no hint text to gate
-- on regardless of source).

BEGIN;

ALTER TABLE bank_layouts
  ADD COLUMN IF NOT EXISTS hints_source TEXT NOT NULL DEFAULT 'auto'
    CHECK (hints_source IN ('auto', 'manual', 'mixed'));

-- Backfill existing populated rows. New rows landed after this
-- migration get the column default ('auto') and the repository
-- upgrades to 'mixed' on the first manual write.
UPDATE bank_layouts
   SET hints_source = 'manual'
 WHERE extraction_hints IS NOT NULL
   AND hints_source = 'auto';

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT hints_source, count(*) FROM bank_layouts GROUP BY hints_source;
--   SELECT bank_name, hints_source, successful_parses
--     FROM bank_layouts
--     WHERE extraction_hints IS NOT NULL
--     ORDER BY last_seen DESC NULLS LAST;
