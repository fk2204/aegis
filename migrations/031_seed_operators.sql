-- Migration 031 — seed the operators table with the three real
-- @commerafunding.com admins, replacing the stale Gmail row.
--
-- Background
-- ----------
-- AEGIS gates user access at the Cloudflare Access layer; the
-- ``operators`` table (migration 022) is not read at runtime today
-- for any authorization decision. The 2026-05-27 access-model audit
-- surfaced two consequences:
--
--   1. The only row present in prod is ``fkozina92@gmail.com``, which
--      is Filip's personal Gmail. Cloudflare Access authenticates
--      Filip as ``filip@commerafunding.com``, so this row would never
--      match a request-context lookup once role enforcement is wired.
--   2. Edward (offboarded, replaced by David) was never inserted into
--      the table, so no row-deactivation is required for him.
--
-- This migration pre-stages the three real operator emails so the
-- role-enforcement work, when it lands, doesn't lock anyone out. All
-- three are seeded as ``admin`` — Commera is a 3-person team, finer-
-- grained roles can be set via direct UPDATE later.
--
-- Idempotency
-- -----------
-- INSERT ... ON CONFLICT (email) DO NOTHING on the unique ``email``
-- column. Safe to re-run. The DELETE of the Gmail row is also safe to
-- re-run (deletes 0 or 1 rows; no error if absent).
--
-- Display names
-- -------------
-- "Filip Kozina" matches the existing seed-comment in migration 022.
-- David and Dima are seeded with their first name only — the operator
-- can UPDATE the row with a full display name at any time. The CHECK
-- constraint requires length >= 1 and a single first name satisfies it.

DELETE FROM operators WHERE email = 'fkozina92@gmail.com';

INSERT INTO operators (email, display_name, role, is_active)
VALUES
  ('filip@commerafunding.com', 'Filip Kozina', 'admin', TRUE),
  ('david@commerafunding.com', 'David',        'admin', TRUE),
  ('dima@commerafunding.com',  'Dima',         'admin', TRUE)
ON CONFLICT (email) DO NOTHING;
