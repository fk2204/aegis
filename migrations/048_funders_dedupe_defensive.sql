-- Defensive dedupe of the funders table by name.
--
-- The funders.name column has a UNIQUE constraint (migration 003), so
-- the DB engine itself prevents duplicate names from landing. This
-- migration is defensive — if the constraint is ever dropped, or if a
-- duplicate slips in via direct SQL bypassing the constraint, this
-- cleans up by keeping the row with the most-populated criteria.
--
-- Idempotent: on a clean table (no duplicates), the DELETE finds zero
-- rows to remove. Safe to re-apply.
--
-- Tie-break ordering for "which row to keep":
--   1. Most-populated criteria (revenue / FICO / max advance / factor
--      / positions / notes count). More data wins.
--   2. Oldest created_at (older row wins — preserves original intent).

WITH ranked AS (
  SELECT
    id,
    name,
    ROW_NUMBER() OVER (
      PARTITION BY name
      ORDER BY
        (CASE WHEN min_monthly_revenue IS NOT NULL THEN 1 ELSE 0 END) +
        (CASE WHEN min_credit_score IS NOT NULL THEN 1 ELSE 0 END) +
        (CASE WHEN max_advance IS NOT NULL THEN 1 ELSE 0 END) +
        (CASE WHEN typical_factor_low IS NOT NULL THEN 1 ELSE 0 END) +
        (CASE WHEN max_positions IS NOT NULL THEN 1 ELSE 0 END) +
        (CASE WHEN length(notes_residual) > 0 THEN 1 ELSE 0 END) +
        (CASE WHEN array_length(excluded_industries, 1) > 0 THEN 1 ELSE 0 END) DESC,
        created_at ASC
    ) AS rn
  FROM funders
)
DELETE FROM funders WHERE id IN (
  SELECT id FROM ranked WHERE rn > 1
);
