-- Migration 011 — enable Row Level Security on all public-schema tables.
--
-- Fixes Supabase Security Advisor lints:
--   * rls_disabled_in_public        (every public table)
--   * sensitive_columns_exposed     (merchants: email/phone/owner_name/
--                                    business_name; transactions: description)
--
-- AEGIS connects to Supabase only via SUPABASE_SERVICE_KEY (service_role),
-- which bypasses RLS by design. Enabling RLS with no policies fully denies
-- the anon and authenticated PostgREST roles, while the backend (service_role)
-- continues to read and write unchanged.
--
-- No policies are added. The system is internal-only behind Cloudflare Access
-- plus bearer-token auth; there is no browser/anon traffic against Supabase.
--
-- IMPORTANT: do NOT add FORCE ROW LEVEL SECURITY. FORCE would apply RLS to
-- service_role as well, which would break every backend query.
--
-- Production already contained 18 public-schema tables on 2026-05-13 (only 7
-- are created by migrations 000–010; the other 11 came from dashboard-created
-- tables and earlier dev artifacts). The DO block below covers every public
-- table, present or future, that still has RLS disabled — idempotent and
-- resilient to drift between local migrations and the live schema.

DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'public' AND rowsecurity = false
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', r.tablename);
  END LOOP;
END $$;

-- Post-apply verification (run separately, expect rowsecurity = true on all):
--   SELECT tablename, rowsecurity
--   FROM pg_tables
--   WHERE schemaname = 'public'
--   ORDER BY tablename;
