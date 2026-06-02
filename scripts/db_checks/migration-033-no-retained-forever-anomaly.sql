-- DESCRIPTION: Anomaly check — every document with a populated storage_path must have a retention_until set. Zero rows = healthy.
-- EXPECT_ROWS: 0

-- A row matching this query is a bug: storage_path was populated without
-- retention_until, meaning the ciphertext blob will be retained forever
-- (the nightly retention sweep skips rows with NULL retention_until).
-- Chunk B's worker writes both columns together via a single atomic
-- UPDATE (persist_storage_metadata); this probe is the regression
-- guard that catches any future code path that splits them.

SELECT id, storage_path, retention_until
FROM public.documents
WHERE storage_path IS NOT NULL
  AND retention_until IS NULL;
