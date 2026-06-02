-- DESCRIPTION: Verify migration 033 — documents storage/retention columns + merchants.deleted_at all present.
-- EXPECT_ROWS: 5

SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    (table_name = 'documents' AND column_name IN (
      'storage_path',
      'sha256_original',
      'encryption_key_version',
      'retention_until'
    ))
    OR
    (table_name = 'merchants' AND column_name = 'deleted_at')
  )
ORDER BY table_name, column_name;
