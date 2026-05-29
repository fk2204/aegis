-- DESCRIPTION: Verify migration 032 — analyses.pattern_analysis column exists with type jsonb.
-- EXPECT_ROWS: 1
--
-- Block 1 of 4 for migration 032 post-apply verification. If this returns 0
-- rows the ALTER TABLE in 032 did not land — STOP and re-apply before any
-- further check.
--
-- Expected row: pattern_analysis | jsonb | YES (nullable is intentional —
-- no backfill, legacy rows stay NULL).
SELECT
  column_name,
  data_type,
  is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'analyses'
  AND column_name = 'pattern_analysis';
