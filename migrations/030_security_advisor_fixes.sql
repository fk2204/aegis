-- Migration 030 — Supabase Security Advisor fixes.
--
-- Resolves the lints surfaced on 2026-05-25 / 2026-05-27:
--
--   ERRORS
--   ------
--   1. rls_disabled_in_public on public.operators
--        Created by migration 022, which post-dated the 011 sweep.
--   2. rls_disabled_in_public on public.schema_migrations
--        Created by scripts/apply_migrations.py (not by a migration),
--        so the 011 sweep never saw it.
--   3. security_definer_view on public.audit_log_by_deal
--        Views run with the creator's privileges unless they opt in to
--        security_invoker. AEGIS uses service_role for every backend
--        call and denies anon/authenticated entirely (no policies on
--        the underlying tables), so this is cosmetic at runtime — but
--        fixing it satisfies the lint and the principle of least
--        privilege.
--
--   WARNINGS
--   --------
--   4. function_search_path_mutable on public.block_decision_modification
--        Pinning search_path prevents a malicious schema-shadowing
--        attack against the immutability trigger.
--   5. function_search_path_mutable on public.updated_at
--        Not created by AEGIS migrations — present in production from
--        a Supabase dashboard / default. The DO block below patches
--        it ONLY if it actually exists; no-op otherwise.
--
-- Not addressed here:
--   * extension_in_public on pg_trgm — unused in AEGIS code; leaving in
--     place rather than moving (extension moves can break dependent
--     objects). Document and move in a later migration if needed.
--   * rls_enabled_no_policy info-level lints — intentional, see the
--     header of migration 011.
--
-- AEGIS connects to Supabase only via service_role, which bypasses
-- RLS by design. Enabling RLS without policies fully denies the anon
-- and authenticated PostgREST roles, exactly the desired behavior.

-- ---------------------------------------------------------------------
-- 1 + 2. Enable RLS on operators + schema_migrations.
-- ---------------------------------------------------------------------

ALTER TABLE public.operators ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------
-- 3. Recreate audit_log_by_deal with security_invoker = true.
--    Body is identical to migration 019; only the view options change.
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW public.audit_log_by_deal
WITH (security_invoker = true) AS
  SELECT
    audit_log.deal_id AS deal_id,
    audit_log.id AS audit_id,
    audit_log.actor,
    audit_log.action,
    audit_log.subject_type,
    audit_log.subject_id,
    audit_log.details,
    audit_log.state_change,
    audit_log.aegis_version,
    audit_log.rule_pack_version,
    audit_log.created_at
  FROM audit_log
  WHERE audit_log.deal_id IS NOT NULL

  UNION ALL

  SELECT
    audit_log.subject_id AS deal_id,
    audit_log.id AS audit_id,
    audit_log.actor,
    audit_log.action,
    audit_log.subject_type,
    audit_log.subject_id,
    audit_log.details,
    audit_log.state_change,
    audit_log.aegis_version,
    audit_log.rule_pack_version,
    audit_log.created_at
  FROM audit_log
  JOIN documents ON documents.id = audit_log.subject_id
  WHERE audit_log.deal_id IS NULL
    AND audit_log.subject_type = 'document'

  UNION ALL

  SELECT
    documents.id AS deal_id,
    audit_log.id AS audit_id,
    audit_log.actor,
    audit_log.action,
    audit_log.subject_type,
    audit_log.subject_id,
    audit_log.details,
    audit_log.state_change,
    audit_log.aegis_version,
    audit_log.rule_pack_version,
    audit_log.created_at
  FROM audit_log
  JOIN documents ON documents.merchant_id = audit_log.subject_id
  WHERE audit_log.deal_id IS NULL
    AND audit_log.subject_type = 'merchant';

-- ---------------------------------------------------------------------
-- 4. Pin search_path on block_decision_modification.
--    CREATE OR REPLACE retains the existing triggers wired in 015.
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.block_decision_modification()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
  RAISE EXCEPTION 'decisions table is append-only; use a new row to supersede';
END;
$$;

-- ---------------------------------------------------------------------
-- 5. Pin search_path on public.updated_at if it exists.
--    AEGIS migrations don't create this function; production has it
--    from a Supabase dashboard / default. Patch in place without
--    altering its body — ALTER FUNCTION ... SET search_path leaves
--    the definition alone. Skipped silently if the function is absent.
-- ---------------------------------------------------------------------

DO $$
DECLARE
  fn_oid OID;
BEGIN
  SELECT p.oid INTO fn_oid
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'public'
    AND p.proname = 'updated_at'
  LIMIT 1;

  IF fn_oid IS NOT NULL THEN
    EXECUTE format(
      'ALTER FUNCTION %s SET search_path = pg_catalog, public',
      fn_oid::regprocedure
    );
  END IF;
END $$;
