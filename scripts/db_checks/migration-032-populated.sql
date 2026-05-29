-- DESCRIPTION: Verify migration 032 chunk 2 — pattern_analysis populated on at least one analyses row.
-- EXPECT_ROWS_MIN: 1
--
-- Supplemental check for migration 032 (chunk 2 write-path
-- verification). After chunk 2 deploys, every NEW upload should land
-- with pattern_analysis populated; legacy rows stay NULL (no backfill
-- per the stage 2 design plan). Run after a deliberate re-parse via
-- scripts/_reparse_one.py + re-upload to confirm the chunk-2 writer
-- is live.
--
-- EXPECT_ROWS_MIN: 1 is the load-bearing assertion — zero rows means
-- either (a) no docs have been parsed since chunk 2 deploy, or (b)
-- the writer is broken. The four chunk-1 checks (column / index /
-- schema_migrations / audit_log) cover the migration body itself;
-- this check covers the code-side write path that chunk 2 added.
--
-- Returns up to 5 most-recently-parsed documents whose analyses row
-- has a populated pattern_analysis DTO, with:
--   * original_filename + parsed_at  -> operator-readable identity
--   * schema_version                 -> confirms DTO is v1
--   * pattern_count                  -> non-zero means real detector output
--                                       (zero would mean an empty PatternAnalysis
--                                        was stored — defensive guard for that
--                                        edge case, not a failure on its own).
--
-- parsed_at lives on documents (migration 000:76), not analyses; join
-- on document_id to surface it. NULLS LAST in ORDER BY because a row
-- can in theory have NULL parsed_at (parse pending / errored), though
-- those won't have an analyses row in the first place — defensive
-- ordering doesn't hurt.
SELECT
  d.original_filename,
  d.parsed_at,
  a.pattern_analysis ->> 'schema_version'              AS schema_version,
  jsonb_array_length(a.pattern_analysis -> 'patterns') AS pattern_count
FROM analyses a
JOIN documents d ON d.id = a.document_id
WHERE a.pattern_analysis IS NOT NULL
ORDER BY d.parsed_at DESC NULLS LAST
LIMIT 5;
