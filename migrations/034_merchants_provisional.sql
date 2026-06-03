-- Migration 034 — merchants.status for the merchant-from-statement flow
--
-- Auto-create provisional merchant at dashboard upload time; finalize
-- it at parse-completion using the statement's account_holder. See
-- docs/AUTO_CREATE_MERCHANT_DESIGN.md (lands alongside this work).
--
-- Three statuses, single column:
--   * provisional          — created at upload, parse not done
--   * needs_manual_naming  — parse done but couldn't extract a name
--                            (blank account_holder, parse exception,
--                            parse cancellation, processor-branch
--                            success). Operator names via intake.
--   * finalized            — has a business_name. Default for every
--                            existing row (all current merchants came
--                            from operator-curated paths).
--
-- Existing rows: all become status='finalized' via the DEFAULT. Their
-- business_name is already NOT NULL (the old schema required it), so
-- the merchants_finalized_has_business_name CHECK passes on every
-- legacy row without any backfill UPDATE.
--
-- Both CHECKs are validated inline (not NOT VALID/VALIDATE-split).
-- Rationale: the merchants table currently has ~2 rows; the
-- defensive split that avoids write locks during VALIDATE is a tool
-- for large tables and has no payoff here. Consistency between the
-- two CHECKs (one inline, one split) was worse than picking one
-- pattern; we pick inline.
--
-- Columns relaxed to NULL:
--   business_name, owner_name, state — provisional and
--   needs_manual_naming rows have none of these at create time. The
--   finalized-state CHECK below brings business_name back as required
--   for the only status where downstream code reads it without a
--   None-guard. owner_name and state stay nullable across all statuses
--   because (a) auto-finalize from the statement intentionally leaves
--   owner_name NULL — copying account_holder (the BUSINESS name) into
--   owner_name (the HUMAN owner) would write wrong-but-filled-looking
--   data into disclosure / funder packets, and (b) the parser doesn't
--   extract state, so requiring state to finalize would block 100% of
--   auto-finalizes. Operator sets both via the existing edit affordance.

BEGIN;

ALTER TABLE public.merchants
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'finalized'
    CHECK (status IN ('provisional', 'needs_manual_naming', 'finalized'));

COMMENT ON COLUMN public.merchants.status IS
  'Lifecycle status. Transient ''provisional'' at upload time → '
  '''finalized'' (clean account_holder from statement) or '
  '''needs_manual_naming'' (blank / parse-fail / processor-branch) '
  'at parse-completion. Default ''finalized'' so existing curated '
  'rows pass the finalized-has-business_name CHECK without backfill.';

ALTER TABLE public.merchants ALTER COLUMN business_name DROP NOT NULL;
ALTER TABLE public.merchants ALTER COLUMN owner_name    DROP NOT NULL;
ALTER TABLE public.merchants ALTER COLUMN state         DROP NOT NULL;

ALTER TABLE public.merchants
  ADD CONSTRAINT merchants_finalized_has_business_name
  CHECK (status <> 'finalized' OR business_name IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_merchants_status_attention
  ON public.merchants (status)
  WHERE status IN ('provisional', 'needs_manual_naming');

COMMIT;
