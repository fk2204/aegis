-- Migration 005 — funders.requires_coj column.
--
-- Adds a boolean flag to the funders table indicating whether the
-- funder's underwriting agreement requires a confession of judgment
-- (CoJ). The matcher hard-declines any pairing of a CoJ-requiring
-- funder with a merchant in a state whose StateRegulation.coj_allowed
-- is false (per docs/compliance/01_california.md, California bans CoJ
-- under Cal. Code Civ. Proc. § 1132 effective 2023-01-01).
--
-- Default false: existing funders are assumed to NOT require CoJ until
-- the operator explicitly sets the flag from the funder's ISO agreement.

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS requires_coj BOOLEAN NOT NULL DEFAULT false;
