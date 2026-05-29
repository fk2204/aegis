-- DESCRIPTION: Verify migration 032 — schema_migrations row written by the 3C-extra runner.
-- EXPECT_ROWS: 1
--
-- Block 3 of 4 for migration 032 post-apply verification. The 3C-extra
-- runner wraps the migration body + this schema_migrations row + the
-- audit_log row in a single transaction; absence here means the apply
-- did not commit cleanly OR the runner crashed mid-transaction (rollback
-- should leave column + index + this row all absent in that case).
--
-- Expected row: filename = '032_analyses_pattern_analysis.sql', sha256
-- matching the dry-run output prefix, applied_by = 'apply_migrations:<user>'.
-- Re-apply attempts would surface as MigrationDriftError on a sha256
-- mismatch rather than a duplicate row.
SELECT
  filename,
  sha256,
  applied_at,
  applied_by
FROM schema_migrations
WHERE filename = '032_analyses_pattern_analysis.sql';
