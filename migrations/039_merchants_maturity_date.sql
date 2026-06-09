-- Migration 039 — merchants.maturity_date (R3.2 follow-up, audit finding H12).
--
-- R3.2 (commit 6102bbc) shipped the renewal-calendar route + the
-- ``list_upcoming_renewals`` accessor, but the accessor returns ``[]`` and
-- logs ``"schema augmentation pending"`` because the underlying column
-- doesn't exist yet. The R3 audit ticketed this gap as H12 — close it by
-- adding the column.
--
-- Per CLAUDE.md SCOPE NOTE + ``.claude/rules/operating-principles.md`` #4
-- ("production database state must be operator-real, not seeded"), this
-- migration is purely additive:
--
--   * No NOT NULL — existing rows stay valid without a backfill.
--   * No DEFAULT — the operator populates maturity_date per-deal at
--     renewal-onboarding time, never via a synthetic seed.
--
-- Idempotent: ``ADD COLUMN IF NOT EXISTS`` is safe to re-run.
--
-- Compliance note: AEGIS does NOT own regulator-facing renewal disclosure
-- issuance; funder partners do (see CLAUDE.md mission statement and
-- ``src/aegis/merchants/repository.py`` ``_STATE_DISCLOSURE_LEAD_DAYS``
-- docstring). The column drives operator visibility only — it never gates
-- a broker-side enforcement action.

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS maturity_date DATE;
