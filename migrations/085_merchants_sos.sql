-- Migration 085 — Secretary of State entity-check columns on merchants.
--
-- Powers the Phase C compliance step (SOS local SQLite cache + Bedrock
-- fallback). Local lookup (instant, $0) wins when the merchant's state
-- is one of the bulk-data states; the Bedrock fallback (~$0.05) covers
-- the rest. Dossier renders a chip per the ``sos_is_active`` /
-- ``sos_status`` combination.
--
-- All columns are additive + nullable + idempotent — re-applying the
-- migration is a no-op against an already-populated table.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS sos_checked_at      timestamptz,
  ADD COLUMN IF NOT EXISTS sos_status          text,
  ADD COLUMN IF NOT EXISTS sos_entity_name     text,
  ADD COLUMN IF NOT EXISTS sos_formation_date  text,
  ADD COLUMN IF NOT EXISTS sos_is_active       boolean,
  ADD COLUMN IF NOT EXISTS sos_data_source     text,
  ADD COLUMN IF NOT EXISTS sos_state_checked   text;

COMMENT ON COLUMN merchants.sos_checked_at IS
  'Timestamp of the most recent SOS lookup. NULL = never checked, scorer triggers ensure_sos_check on next score.';
COMMENT ON COLUMN merchants.sos_status IS
  'Free-form status string the registry returned (e.g. "ACTIVE", "DISSOLVED", "INACTIVE").';
COMMENT ON COLUMN merchants.sos_entity_name IS
  'Entity name as recorded by the registry (may differ from business_name — operator review signal).';
COMMENT ON COLUMN merchants.sos_formation_date IS
  'Formation / incorporation date as recorded by the registry, ISO-format string (parser tolerates state-specific formats).';
COMMENT ON COLUMN merchants.sos_is_active IS
  'True if the registry treats the entity as active; False if dissolved/withdrawn; NULL if unknown.';
COMMENT ON COLUMN merchants.sos_data_source IS
  'Source token: "local_db:<STATE>" (bulk-data hit), "bedrock_fallback" (Bedrock web search), "no_data" (state not covered + Bedrock returned nothing).';
COMMENT ON COLUMN merchants.sos_state_checked IS
  'The merchant.state captured at check time. Lets the operator spot when state changes between checks.';

COMMIT;
