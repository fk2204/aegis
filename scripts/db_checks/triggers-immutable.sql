-- DESCRIPTION: All immutability triggers across the schema (currently only decisions).
-- EXPECT_ROWS_MIN: 2
--
-- Broader view than block-4-triggers-exist: returns every trigger whose
-- function name matches the `block_*_modification` convention. Future
-- migrations that add append-only tables (audit_log, snapshots, etc.) will
-- show up here automatically.
SELECT
  tgname AS trigger_name,
  c.relname AS table_name,
  p.proname AS function_name
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE p.proname LIKE 'block_%_modification'
  AND NOT t.tgisinternal
ORDER BY c.relname, tgname;
