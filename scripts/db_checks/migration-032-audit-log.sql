-- DESCRIPTION: Verify migration 032 — audit_log row written by the 3C-extra runner.
-- EXPECT_ROWS: 1
--
-- Block 4 of 4 for migration 032 post-apply verification. The 3C-extra
-- runner writes one audit_log row per applied migration; the row carries
-- filename, sha256, target, started_at, finished_at, and aegis_version
-- (short git SHA at runner invocation) inside the details JSONB so the
-- INSERT is forward-compatible with pre-019 and post-019 audit_log
-- schemas. See scripts/apply_migrations.py:_AUDIT_LOG_INSERT_SQL.
--
-- Expected row: actor = 'apply_migrations:<user>' matching the
-- schema_migrations.applied_by value, details->>'target' = 'prod',
-- details->>'sha256' matching the schema_migrations.sha256 value,
-- details->>'aegis_version' = short git SHA at apply time.
SELECT
  created_at,
  actor,
  details->>'aegis_version' AS aegis_version,
  details->>'filename'      AS filename,
  details->>'target'        AS target,
  details->>'sha256'        AS sha256,
  details->>'started_at'    AS started_at,
  details->>'finished_at'   AS finished_at
FROM audit_log
WHERE action = 'migration_applied'
  AND details->>'filename' = '032_analyses_pattern_analysis.sql'
ORDER BY created_at DESC
LIMIT 1;
