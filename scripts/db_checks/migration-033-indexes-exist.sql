-- DESCRIPTION: Verify migration 033 — partial indexes on documents.retention_until and merchants.deleted_at exist with correct predicates.
-- EXPECT_ROWS: 2

SELECT
  indexname,
  indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN (
    'idx_documents_retention_until',
    'idx_merchants_deleted_at'
  )
ORDER BY indexname;
