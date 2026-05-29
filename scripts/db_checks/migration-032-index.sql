-- DESCRIPTION: Verify migration 032 — analyses_pattern_analysis_gin index exists with jsonb_path_ops opclass.
-- EXPECT_ROWS: 1
--
-- Block 2 of 4 for migration 032 post-apply verification. The GIN index
-- enables future analytics queries against pattern_analysis (e.g. "find
-- every analysis whose patterns include unauthorized_withdrawal_dispute").
-- Not load-bearing in stage 2, but if absent the cost of adding it after
-- data lands is a full table rewrite.
--
-- Expected row: indexname = analyses_pattern_analysis_gin, indexdef
-- containing 'USING gin (pattern_analysis jsonb_path_ops)'.
SELECT
  indexname,
  indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename = 'analyses'
  AND indexname = 'analyses_pattern_analysis_gin';
