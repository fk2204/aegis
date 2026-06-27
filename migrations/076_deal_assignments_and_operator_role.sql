-- Migration 076 — operators.role widened to admin/underwriter/viewer,
-- plus deal_assignments table.
--
-- Two DDL changes folded into one migration so the role surface and the
-- per-merchant assignment surface ship atomically. Both are gated on
-- the same operators table, and Commit 2 (deal-assignment routes) needs
-- the widened CHECK constraint to land first so a viewer-role operator
-- can be assigned without DB-level rejection.
--
-- Role widening rationale
-- -----------------------
-- Migration 022 introduced ``operators.role`` with CHECK constraint
-- ``role IN ('underwriter', 'compliance_reviewer', 'admin')``. The
-- product-level role matrix has since collapsed to three roles:
--
--   * ``admin``       — full surface; the operator-owner role.
--   * ``underwriter`` — default; can score deals, submit to funders,
--                      record outcomes, override recommendations.
--   * ``viewer``      — read-only dossier access; cannot mutate.
--
-- ``compliance_reviewer`` rows (if any exist) keep their meaning at the
-- application layer (the role-gate dependency treats them as viewer).
-- The CHECK constraint is widened to accept all four values so existing
-- rows don't fail the constraint after the migration runs; new
-- operators are seeded with one of the three product roles.
--
-- Deal assignments
-- ----------------
-- Tracks "who owns this deal". One assignee per merchant at a time
-- (enforced by the UNIQUE constraint on ``merchant_id``). Re-assignment
-- is a single UPDATE; un-assignment is a DELETE. The assignment chip on
-- the dossier header reads from this table; ``/ui/today`` and
-- ``/ui/merchants`` accept ``?assignee=me`` and filter by
-- ``operator_id``.
--
-- The audit_log captures the reassignment via
-- ``merchant.assignment.created`` / ``merchant.assignment.removed``
-- (no separate update — replace = delete + create at the application
-- layer for simplicity).

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Widen operators.role CHECK constraint.
--
-- Postgres doesn't allow ALTER on a CHECK constraint in place; drop
-- the existing one (named anonymously by migration 022) and re-create
-- under a stable name so future migrations have a target.
-- ---------------------------------------------------------------------

-- Drop ALL CHECK constraints on operators whose definition references
-- the ``role`` column. Postgres normalizes ``role IN (...)`` to
-- ``role = ANY (ARRAY[...])`` in ``pg_get_constraintdef()``, so the
-- pattern must match the normalized form — using ILIKE '%role%' is
-- the simplest match that covers both anonymous (migration 022) and
-- named (``operators_role_check`` from a prior run of this migration)
-- shapes. ``operators`` has no non-role CHECK constraints, so the
-- broad match is safe. Loop guarantees idempotence — re-runs find
-- nothing to drop and the ADD below lands the fresh constraint.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT conname FROM pg_constraint
    WHERE conrelid = 'operators'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%role%'
  LOOP
    EXECUTE format('ALTER TABLE operators DROP CONSTRAINT %I', r.conname);
  END LOOP;
END$$;

ALTER TABLE operators
  ADD CONSTRAINT operators_role_check
  CHECK (role IN ('underwriter', 'compliance_reviewer', 'admin', 'viewer'));

-- ---------------------------------------------------------------------
-- 2. deal_assignments table.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS deal_assignments (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id  UUID NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
  operator_id  UUID NOT NULL REFERENCES operators(id),
  assigned_by  UUID NOT NULL REFERENCES operators(id),
  assigned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- One assignee per merchant at a time. Re-assignment = DELETE + INSERT
  -- at the application layer (cleaner audit trail than an UPDATE that
  -- silently overwrites who-was-assigned-before).
  UNIQUE (merchant_id)
);

-- Per-operator lookup powers the "My deals" filter on /ui/today and
-- /ui/merchants. Indexed without a partial WHERE because we expect the
-- vast majority of rows to be queried; bloat is bounded by the UNIQUE
-- constraint on merchant_id.
CREATE INDEX IF NOT EXISTS idx_deal_assignments_operator_id
  ON deal_assignments (operator_id);

COMMIT;
