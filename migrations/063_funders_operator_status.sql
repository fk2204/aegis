-- Migration 063 — funders.operator_status: model live funder appetite.
--
-- ``funders.active`` is a binary "this funder is in the system or not"
-- flag that's been used to soft-delete funders. It does NOT capture the
-- shorter-cycle states an operator actually deals with day-to-day:
--
--   * paused                — funder isn't accepting submissions this
--                             week (e.g. holiday, capital pause).
--                             Hard-fail in the matcher.
--   * first_position_only   — funder will only fund a deal with no
--                             existing MCA positions. Hard-fail when
--                             the deal's mca_positions >= 1.
--   * selective             — funder is open but cherry-picking;
--                             surface as a soft concern.
--   * active                — normal matching. Default for every row.
--
-- These changes happen multiple times per month per funder; rolling
-- them into ``active`` would force a destructive edit to opt a funder
-- out of matching, then a manual re-enable later. ``operator_status``
-- captures the gradient cleanly: the dashboard surfaces a chip and
-- the matcher emits a clear reason on the fail/concern.
--
-- Default ``'active'`` preserves matcher behavior for every existing
-- row. The CHECK constraint enforces the allowed values at the DB
-- boundary so a hand-edit via the Supabase UI can't push an invalid
-- value that the matcher would silently ignore.

BEGIN;

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS operator_status TEXT
    NOT NULL DEFAULT 'active'
    CHECK (operator_status IN ('active','paused','first_position_only','selective'));

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT operator_status, count(*) FROM funders GROUP BY operator_status;
--   SELECT name, operator_status FROM funders ORDER BY operator_status, name;
