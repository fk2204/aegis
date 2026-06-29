-- Migration 100 — add 'disqualified' to merchants.status CHECK constraint.
--
-- AEGIS uses MerchantStatus to gate the dossier render + routing
-- decisions; before this migration the allowed values were
-- {provisional, needs_manual_naming, finalized} (see migration 034 chunk B).
-- We now need a fourth value for merchants the operator has explicitly
-- declined as un-fundable — TCPA serial litigants, OFAC matches that
-- can't be resolved, etc. The detector layer (e.g.
-- aegis.parser.patterns.detect_tcpa_litigant) flags the merchant; the
-- operator confirms; the status flips to 'disqualified' and the
-- dossier renders with a permanent decline banner.
--
-- DROP IF EXISTS + ADD pattern matches the migration-017 / migration-091
-- recipe for safely renaming an in-place CHECK constraint without losing
-- the gate during the window between drop and add.

ALTER TABLE merchants DROP CONSTRAINT IF EXISTS merchants_status_check;

ALTER TABLE merchants
  ADD CONSTRAINT merchants_status_check
  CHECK (status IN (
    'provisional',
    'needs_manual_naming',
    'finalized',
    'disqualified'
  ));
