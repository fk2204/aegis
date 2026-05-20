-- Migration 022 — operators table + roles for multi-operator readiness.
--
-- Phase 11 task #8 (master plan §21). Even though AEGIS today runs
-- solo-operator behind Cloudflare Access, the schema gets the roles
-- enum + lookup table NOW so the audit_log.actor + the future role-
-- gated endpoints have something to reference once the second
-- operator is invited.
--
-- Why a table, not just an enum:
--   * Cloudflare Access identifies operators by email. The application
--     reads that email from a request header (CF-Access-Authenticated-
--     User-Email) and looks up the role here. A flat enum on every
--     audit_log row would burn schema churn every time a new role
--     classifier is added.
--   * The operator lookup needs a `display_name` separate from email
--     so the UI doesn't leak the email PII into surfaces a borrower
--     might see (rare today but planned for funder-facing dossier
--     downloads).
--
-- Roles
-- -----
--   underwriter         — default. Can score deals, request funder
--                         matches, run Zoho push. NO database admin
--                         access.
--   compliance_reviewer — can approve overrides + view audit_log_by_deal;
--                         scoped narrower than admin.
--   admin               — full surface; the operator-owner role.
--
-- Adding a new operator is the operator-zero-touch ``operators``
-- INSERT done via the dashboard's Settings page (to be wired in
-- a follow-up, but the schema below supports it today).
--
-- Backwards compatibility
-- -----------------------
-- audit_log.actor was a TEXT column from migration 000; it stays.
-- This migration adds a NEW column ``actor_email`` so existing rows
-- (where actor was "api"/"worker"/etc) keep their meaning. New
-- rows from request-context paths populate both: actor='underwriter'
-- (the resolved role) and actor_email=<the cf-access email>.

-- Operators table.
CREATE TABLE IF NOT EXISTS operators (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT NOT NULL UNIQUE
    CHECK (email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  display_name TEXT NOT NULL CHECK (length(display_name) >= 1),
  role TEXT NOT NULL DEFAULT 'underwriter'
    CHECK (role IN ('underwriter', 'compliance_reviewer', 'admin')),
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_operators_role
  ON operators (role)
  WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_operators_email_lower
  ON operators (lower(email));

-- Extend audit_log with the operator email column. ADD COLUMN IF NOT
-- EXISTS keeps this idempotent. The column is nullable because legacy
-- rows (and system-actor rows like 'worker'/'api') don't have a
-- person behind them.
ALTER TABLE audit_log
  ADD COLUMN IF NOT EXISTS actor_email TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_actor_email
  ON audit_log (actor_email)
  WHERE actor_email IS NOT NULL;

-- Seed the operator-owner if the table is empty AND the OPERATOR_OWNER_EMAIL
-- substitution is present. Migrations don't substitute env vars, so this
-- INSERT is deliberately commented out — the operator runs it manually
-- after the migration with their actual email:
--
--   INSERT INTO operators (email, display_name, role)
--   VALUES ('filip@commerafunding.com', 'Filip Kozina', 'admin');
