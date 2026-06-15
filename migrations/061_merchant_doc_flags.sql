-- Migration 061 — merchant document-on-file flags for the completeness
-- checker (Feature 2 — 2026-06-15 operator directive).
--
-- The "Submit to Funder" button on the dossier needs a pre-flight check
-- against the top-matched funder's ``conditional_requirements``: if the
-- funder requires a voided check, a copy of the owner's driver's
-- license, or N months of bank statements, those documents must be on
-- file before the operator can submit. The flags below capture the
-- operator's confirmation that each of those documents has been
-- collected. ``conditional_requirements`` itself is free-text per
-- funder (tuple of strings; see ``aegis.funders.models.FunderRow``);
-- the checker scans the strings for the patterns these flags map to.
--
-- All three columns default to "not on file" so a row that landed
-- before this migration safely surfaces as "operator must check the
-- box" rather than silently passing the completeness gate.
--
-- Why three columns instead of a JSONB document-checklist:
--   * The dossier UI renders chips per-flag — JSONB would force a
--     server-side spread on every dossier render.
--   * Operator updates land via a per-flag toggle (one POST per
--     checkbox) — JSONB would force read-modify-write semantics with
--     a race against concurrent toggles.
--   * The set of flags is small (3) and changes rarely. A JSONB shape
--     is overkill here.
--
-- bank_statements_months is INT >= 0 so the comparison against
-- "last N months" requirements is direct integer math; ``0`` is the
-- default (no statements yet on file) and matches the historical
-- absence-as-zero behaviour the matcher would have inferred.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS voided_check_on_file BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS drivers_license_on_file BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS bank_statements_months INT NOT NULL DEFAULT 0
    CHECK (bank_statements_months >= 0);

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT
--     count(*) FILTER (WHERE voided_check_on_file)      AS voided_check_yes,
--     count(*) FILTER (WHERE drivers_license_on_file)   AS dl_yes,
--     count(*) FILTER (WHERE bank_statements_months>=4) AS four_plus_months
--   FROM merchants;
