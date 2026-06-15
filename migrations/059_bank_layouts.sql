-- Migration 059 — bank_layouts table for layout-learning hints.
--
-- When the parser successfully extracts a bank statement (parse_status
-- in ('proceed', 'review')), the pipeline records a layout fingerprint
-- against the bank's display name and bumps a success counter. After
-- ``HINTS_AVAILABLE_THRESHOLD`` (3) successful parses on the same bank,
-- the operator can author free-form ``extraction_hints`` text that the
-- pipeline injects verbatim into the Bedrock extraction system prompt
-- on subsequent parses of statements from that bank.
--
-- The table is operator-curated metadata, NOT merchant-keyed PII:
--   * ``bank_name``         — bank display string (e.g. "Chase",
--                             "Bank of America"). Stored as the
--                             operator (or first successful parse) saw
--                             it; query-side lookup is
--                             case-insensitive (see repository).
--   * ``layout_fingerprint`` — JSONB describing observable layout
--                             properties (transaction_count,
--                             has_running_balance, page_count,
--                             currency). NEVER contains merchant
--                             identifiers, account holder names, or
--                             transaction descriptions. The fingerprint
--                             is merged on success (new keys win) so
--                             multi-bank-layout coverage grows over
--                             time without per-merchant separation.
--   * ``successful_parses`` — monotone counter; gates ``extraction_hints``
--                             availability via the 3-parse threshold so
--                             a one-off success doesn't drive prompt
--                             changes for an entire bank.
--   * ``extraction_hints``  — operator free-text. Appended verbatim to
--                             the Bedrock system prompt under a
--                             "Layout hints from prior successful parses
--                             of this bank:" header.
--   * ``last_seen``         — TIMESTAMPTZ of the most recent successful
--                             parse on this bank. Drives the dashboard
--                             ORDER BY (NULLS LAST so primed-but-never-
--                             parsed rows sort to the bottom).
--
-- Status lifecycle: monotonic. ``successful_parses`` only ever increases;
-- there is no decrement / rollback path. ``set_hints`` is the one write
-- the operator UI exposes; ``upsert_success`` is the one write the
-- parser pipeline exposes. The two never collide on the same column.
--
-- search_path is pinned to match the migration 030 hardening pass, as
-- with migration 057's trigger function. No trigger here — application
-- code stamps ``last_seen`` explicitly on every upsert and the operator
-- ``set_hints`` path doesn't touch it.

BEGIN;

CREATE TABLE IF NOT EXISTS bank_layouts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bank_name TEXT NOT NULL UNIQUE,
  layout_fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,
  successful_parses INT NOT NULL DEFAULT 0 CHECK (successful_parses >= 0),
  extraction_hints TEXT,
  last_seen TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS bank_layouts_last_seen_idx
  ON bank_layouts (last_seen DESC);

-- Match migrations 011 / 013 / 057 — service_role bypasses RLS; anon /
-- authenticated are denied. No policies = full deny for PostgREST.
ALTER TABLE bank_layouts ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT count(*) FROM bank_layouts;
--   SELECT bank_name, successful_parses, last_seen FROM bank_layouts
--     ORDER BY last_seen DESC NULLS LAST;
