-- Migration 058 — merchants.notes column.
--
-- Operator-facing free-form notes about a merchant. Shown at the top of
-- the dossier (below the masthead) so the underwriter sees the running
-- context — broker-source quirks, prior decline reasons, pricing
-- discussions, etc. — before scanning the score and pattern findings.
--
-- Display contract (application side, NOT enforced by the DB):
--   * Append-only from the UI. The dossier textarea POSTs to
--     POST /ui/merchants/{merchant_id}/notes which prepends a single
--     line of the form ``[YYYY-MM-DD HH:MM UTC] <new text>`` to the
--     existing ``notes`` value before writing back. Operators get the
--     timestamped history rendering for free out of the same column.
--   * NULL means "no notes ever entered" — distinct from "" which would
--     mean "operator cleared the notes". We never write "" through the
--     route, but the column allows both for SQL-tool flexibility.
--
-- No CHECK constraint on length: operator workflow may paste long
-- pricing discussions or broker-call transcripts. The dossier render
-- side handles the display ergonomics (no truncation by default;
-- monospace for the timestamp prefix).

ALTER TABLE merchants
  ADD COLUMN notes TEXT NULL;

COMMENT ON COLUMN merchants.notes IS
  'Operator-curated free-form notes about the merchant. Rendered at top '
  'of dossier. Application appends timestamped lines via '
  'POST /ui/merchants/{id}/notes; SQL-direct edits are allowed but '
  'bypass the timestamp prefix convention.';
