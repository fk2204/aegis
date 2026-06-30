-- Migration 101 — Supabase Security Advisor RLS + Security-Definer-View remediation.
--
-- Background
-- ----------
-- The Supabase Security Advisor flagged 10 errors on 2026-06-30:
--
--   * 8 tables with ROW LEVEL SECURITY DISABLED on schema ``public``:
--       - llm_costs
--       - probe_review_verdicts
--       - notifications
--       - deal_assignments
--       - corpus_documents
--       - tax_returns
--       - ar_aging_reports
--       - override_outcome_links
--     Each row in these tables is currently reachable by any caller with
--     anon / authenticated JWT credentials at the PostgREST API surface,
--     bypassing the AEGIS FastAPI backend entirely. We've been relying on
--     "no one knows the API URL" rather than enforced access control.
--
--   * 2 SECURITY DEFINER views on schema ``public``:
--       - scoring_disagreements_open (migration 038)
--       - deals (migration 012)
--     Both views default to ``security_invoker = false`` which evaluates
--     the underlying SELECT with the OWNER's permissions instead of the
--     caller's. This means a caller who can SELECT the view bypasses the
--     RLS on the base tables it joins — even though those base tables
--     have RLS enabled.
--
-- Posture (matches migration 011 / 015 / 037)
-- ------------------------------------------
-- AEGIS connects via ``SUPABASE_SERVICE_KEY`` (the ``service_role`` JWT)
-- which is BYPASSRLS by Supabase's default role config. RLS-enabling
-- the 8 tables and flipping the 2 views to ``security_invoker`` does
-- NOT change application behavior — every existing read/write through
-- the backend keeps working. The change CLOSES the anon / authenticated
-- exposure that the Advisor flagged.
--
-- Defense-in-depth
-- ----------------
-- For each table we also add an explicit ``deny_all_anon`` policy that
-- returns ``USING (false)`` for the ``anon`` and ``authenticated`` roles.
-- This is belt-and-suspenders — Supabase's default role config already
-- denies these roles when RLS is enabled with no policies — but the
-- explicit deny:
--   1. Survives a future drift where someone adds an overly-broad policy
--      and forgets to add a matching deny.
--   2. Makes the intent legible when reading the table's policies in
--      Supabase Studio (a missing policy looks like an oversight; an
--      explicit deny looks intentional).
--
-- Idempotency
-- -----------
-- ``ALTER TABLE … ENABLE ROW LEVEL SECURITY`` is idempotent (running it
-- twice is a no-op when RLS is already enabled). ``CREATE POLICY`` is
-- NOT idempotent, so we wrap each with ``DROP POLICY IF EXISTS`` first.
-- Same pattern as migrations 011 / 015 / 070.
--
-- View ALTERs use ``ALTER VIEW … SET (security_invoker = true)`` per
-- the Postgres 15+ syntax (Supabase runs PG 15+ on every project).

-- ─────────────────────────────────────────────────────────────────────
-- 1. Enable RLS on the 8 exposed tables
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE public.llm_costs              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.probe_review_verdicts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.deal_assignments       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corpus_documents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tax_returns            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ar_aging_reports       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.override_outcome_links ENABLE ROW LEVEL SECURITY;

-- ─────────────────────────────────────────────────────────────────────
-- 2. Explicit deny-all policies for anon + authenticated (belt &
--    suspenders — service_role bypasses RLS so the app keeps working).
-- ─────────────────────────────────────────────────────────────────────

DROP POLICY IF EXISTS deny_all_anon ON public.llm_costs;
CREATE POLICY deny_all_anon ON public.llm_costs
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

DROP POLICY IF EXISTS deny_all_anon ON public.probe_review_verdicts;
CREATE POLICY deny_all_anon ON public.probe_review_verdicts
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

DROP POLICY IF EXISTS deny_all_anon ON public.notifications;
CREATE POLICY deny_all_anon ON public.notifications
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

DROP POLICY IF EXISTS deny_all_anon ON public.deal_assignments;
CREATE POLICY deny_all_anon ON public.deal_assignments
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

DROP POLICY IF EXISTS deny_all_anon ON public.corpus_documents;
CREATE POLICY deny_all_anon ON public.corpus_documents
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

DROP POLICY IF EXISTS deny_all_anon ON public.tax_returns;
CREATE POLICY deny_all_anon ON public.tax_returns
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

DROP POLICY IF EXISTS deny_all_anon ON public.ar_aging_reports;
CREATE POLICY deny_all_anon ON public.ar_aging_reports
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

DROP POLICY IF EXISTS deny_all_anon ON public.override_outcome_links;
CREATE POLICY deny_all_anon ON public.override_outcome_links
  FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);

-- ─────────────────────────────────────────────────────────────────────
-- 3. Flip the two SECURITY DEFINER views to security_invoker.
--    The base tables (documents / merchants / analyses /
--    scoring_shadow_disagreements) already have RLS enabled and
--    deny anon/authenticated — security_invoker just propagates
--    that posture through the view layer.
-- ─────────────────────────────────────────────────────────────────────

ALTER VIEW public.deals                       SET (security_invoker = true);
ALTER VIEW public.scoring_disagreements_open  SET (security_invoker = true);

-- ─────────────────────────────────────────────────────────────────────
-- 4. Verification queries (run separately to confirm the migration
--    achieved its goal — not part of the migration itself).
-- ─────────────────────────────────────────────────────────────────────

-- All 8 tables should show rowsecurity = true:
--
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
--   AND tablename IN (
--     'llm_costs', 'probe_review_verdicts', 'notifications',
--     'deal_assignments', 'corpus_documents', 'tax_returns',
--     'ar_aging_reports', 'override_outcome_links'
--   )
-- ORDER BY tablename;
--
-- Each table should carry exactly one policy named deny_all_anon:
--
-- SELECT tablename, policyname, roles, cmd, qual
-- FROM pg_policies
-- WHERE schemaname = 'public'
--   AND tablename IN (
--     'llm_costs', 'probe_review_verdicts', 'notifications',
--     'deal_assignments', 'corpus_documents', 'tax_returns',
--     'ar_aging_reports', 'override_outcome_links'
--   )
-- ORDER BY tablename;
--
-- Both views should show options = {security_invoker=true}:
--
-- SELECT relname, reloptions
-- FROM pg_class
-- WHERE relname IN ('deals', 'scoring_disagreements_open')
--   AND relkind = 'v';
--
-- The Supabase Advisor count should drop from 10 to 0 (or near-0 —
-- any remaining warnings should be unrelated to this migration).
