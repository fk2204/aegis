-- Migration 080 — product_type ENUM + columns on merchants / decisions / funder_note_submissions.
--
-- Commera is broadening from pure revenue-based-financing (MCA) to six
-- lending products. Every deal henceforth carries a ``product_type``
-- that drives offer sizing (Phase A Agent 8), narrator framing (Phase A
-- Agent 9), and funder matching (Phase A Agent 9). Existing rows are
-- backfilled to ``'revenue_based'`` because that is truthfully what
-- Commera offered exclusively before this migration (per AEGIS
-- operating-principle 4 — no fabricated defaults; the default reflects
-- pre-migration reality).
--
-- The ENUM is idempotent under partial-apply retries via the standard
-- ``DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL`` guard. The
-- ALTER TABLE ... ADD COLUMN ... IF NOT EXISTS pattern keeps the
-- migration safe if half-applied on a previous run.

BEGIN;

-- 1. Create the ENUM type (idempotent).
DO $$
BEGIN
  CREATE TYPE product_type AS ENUM (
    'revenue_based',
    'business_loan',
    'line_of_credit',
    'equipment',
    'asset_based',
    'receivables'
  );
EXCEPTION
  WHEN duplicate_object THEN NULL;
END
$$;

-- 2. Add the column to the three tables that carry it.
--
-- DEFAULT 'revenue_based' is applied as the column lands so existing
-- rows are backfilled in one step (no separate UPDATE pass needed).
-- NOT NULL is safe because every row gets the default. The default
-- stays on the column going forward so older callers (e.g. tests that
-- construct a row without specifying product_type) keep working.

ALTER TABLE public.merchants
  ADD COLUMN IF NOT EXISTS product_type product_type NOT NULL DEFAULT 'revenue_based';

ALTER TABLE public.decisions
  ADD COLUMN IF NOT EXISTS product_type product_type NOT NULL DEFAULT 'revenue_based';

ALTER TABLE public.funder_note_submissions
  ADD COLUMN IF NOT EXISTS product_type product_type NOT NULL DEFAULT 'revenue_based';

COMMENT ON COLUMN public.merchants.product_type IS
  'Commera lending product this merchant is being underwritten for.
   Defaults to revenue_based (the pre-migration-080 universal value).
   Drives offer sizing, narrator framing, and funder matching.';

COMMENT ON COLUMN public.decisions.product_type IS
  'Product type at decision time. Captured in the immutable snapshot
   so a re-product of the same merchant later does not retroactively
   change what the prior decision was made against.';

COMMENT ON COLUMN public.funder_note_submissions.product_type IS
  'Product type at submission time. A merchant whose product_type
   changes between submissions logs each shape distinctly.';

-- 3. Audit-row for the schema change itself (CLAUDE.md compliance:
--    every state change writes to audit_log).
INSERT INTO audit_log (actor, action, subject_type, subject_id, details)
VALUES (
  'migration_080',
  'schema.product_type_introduced',
  'table',
  NULL,
  jsonb_build_object(
    'enum_name', 'product_type',
    'enum_values', jsonb_build_array(
      'revenue_based',
      'business_loan',
      'line_of_credit',
      'equipment',
      'asset_based',
      'receivables'
    ),
    'columns_added', jsonb_build_array(
      'merchants.product_type',
      'decisions.product_type',
      'funder_note_submissions.product_type'
    ),
    'default_value', 'revenue_based',
    'backfill', 'implicit_via_default'
  )
);

COMMIT;
