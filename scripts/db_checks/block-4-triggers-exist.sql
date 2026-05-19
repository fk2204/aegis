-- DESCRIPTION: Confirm decisions table immutability triggers are installed (migration 015).
-- EXPECT_ROWS: 2
--
-- Block 4 of the verification protocol. If this returns 0 rows the decisions
-- table is mutable — STOP and re-apply migration 015 before any other check.
-- Expected rows: decisions_no_update, decisions_no_delete on `decisions`.
SELECT
  tgname AS trigger_name,
  c.relname AS table_name,
  pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE c.relname = 'decisions'
  AND tgname IN ('decisions_no_update', 'decisions_no_delete')
ORDER BY tgname;
