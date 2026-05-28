-- DESCRIPTION: Verify migration 030 — Supabase Security Advisor fixes landed.
-- EXPECT_ROWS: 4
--
-- One row per fixed object. If a row is missing the fix did not land for that
-- object. Function rows include proconfig so the search_path pin is visible.
--
--   1. operators                        — rowsecurity=true
--   2. schema_migrations                — rowsecurity=true
--   3. audit_log_by_deal                — reloptions contain security_invoker=true
--   4. block_decision_modification      — proconfig contains a search_path=... entry
SELECT 'operators' AS object, 'rls_enabled' AS attribute, rowsecurity::text AS value
FROM pg_tables
WHERE schemaname = 'public' AND tablename = 'operators' AND rowsecurity = true

UNION ALL

SELECT 'schema_migrations', 'rls_enabled', rowsecurity::text
FROM pg_tables
WHERE schemaname = 'public' AND tablename = 'schema_migrations' AND rowsecurity = true

UNION ALL

SELECT 'audit_log_by_deal', 'reloptions', array_to_string(c.reloptions, ',')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname = 'audit_log_by_deal'
  AND c.reloptions IS NOT NULL
  AND 'security_invoker=true' = ANY(c.reloptions)

UNION ALL

SELECT 'block_decision_modification', 'proconfig', array_to_string(p.proconfig, ',')
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname = 'block_decision_modification'
  AND p.proconfig IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM unnest(p.proconfig) cfg WHERE cfg LIKE 'search_path=%'
  )

ORDER BY object;
