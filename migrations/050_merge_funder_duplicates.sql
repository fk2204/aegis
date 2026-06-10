-- Merge near-duplicate funder rows that escaped migration 048's exact-name dedupe.
--
-- Audit on 2026-06-10 surfaced two near-duplicate pairs that slipped past
-- the UNIQUE constraint because the names differ slightly:
--
--   "Logic Advance"        ← canonical per manual §2 / Quick Reference
--   "Logic Advance Group"  ← same funder; "Group" suffix added by an
--                            earlier extraction path
--
--   "SwiftSource Funding"  ← canonical per manual §4 (camelCase 'S' in
--                            second position, matches the funder's own
--                            branding)
--   "Swiftsource Funding"  ← same funder; lowercase 's' in second position
--
-- Both pairs reference the same legal entity. Keep the canonical name
-- (matches manual exactly + matches what migration 049 populated tiers
-- against) and delete the variant.
--
-- Idempotent: DELETE finds zero rows if the variants are already absent.

DELETE FROM funders WHERE name IN (
  'Logic Advance Group',
  'Swiftsource Funding'
);
