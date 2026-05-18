-- Migration 019 — extend audit_log (master plan §9.6)
--
-- Adds five columns + creates the per-deal view used by the new
-- /audit/deal/{id} route in src/aegis/api/routes/audit.py.
--
-- New columns:
--   * deal_id (UUID, indexed) — references documents(id). The application
--     layer writes documents.id here; the composite deal_id string is
--     materialized on read by aegis.deals.models.format_deal_id.
--   * state_change (JSONB) — {"before": {...}, "after": {...}} payload
--     for state transitions. Optional; older rows have NULL.
--   * actor (TEXT) — DUPLICATE of the existing actor column for the
--     master plan's §9.6 spec. We already have a non-null actor on every
--     row from migration 000, so this is a NO-OP confirmation pass that
--     uses ADD COLUMN IF NOT EXISTS.
--   * aegis_version (TEXT) — git rev short hash at write time. Optional.
--   * rule_pack_version (TEXT) — scoring rule pack SHA. Optional.
--
-- View `audit_log_by_deal(deal_id)` powers the audit UI: every event
-- attributable to a given deal in chronological order. Three union
-- sources because legacy rows used `subject_id` to point at the merchant
-- or document; new rows fill `deal_id` directly. The view normalizes.

-- Extend audit_log with the new columns. ADD COLUMN IF NOT EXISTS keeps
-- the migration idempotent against an audit_log already touched by
-- earlier code paths.
ALTER TABLE audit_log
  ADD COLUMN IF NOT EXISTS deal_id UUID,
  ADD COLUMN IF NOT EXISTS state_change JSONB,
  ADD COLUMN IF NOT EXISTS aegis_version TEXT,
  ADD COLUMN IF NOT EXISTS rule_pack_version TEXT;

-- The actor column was created NOT NULL by migration 000 already; this
-- is a no-op guard for environments where the column might have been
-- dropped manually.
ALTER TABLE audit_log
  ADD COLUMN IF NOT EXISTS actor TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_deal ON audit_log (deal_id)
  WHERE deal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor)
  WHERE actor IS NOT NULL;

-- Per-deal audit view. Resolves three event sources into one stream:
--   1. Rows with deal_id set directly (post-migration writes).
--   2. Rows whose subject_type='document' + subject_id maps to a deal
--      via documents.id.
--   3. Rows whose subject_type='merchant' + subject_id maps to a deal
--      via documents.merchant_id. One merchant may have multiple deals;
--      events at the merchant level surface against every deal for that
--      merchant — operator-facing context.
--
-- All three sources are unioned and re-keyed by the resolved deal_id
-- (which is documents.id). The view never invents a deal_id: rows that
-- can't resolve to a document are excluded.

CREATE OR REPLACE VIEW audit_log_by_deal AS
  SELECT
    audit_log.deal_id AS deal_id,
    audit_log.id AS audit_id,
    audit_log.actor,
    audit_log.action,
    audit_log.subject_type,
    audit_log.subject_id,
    audit_log.details,
    audit_log.state_change,
    audit_log.aegis_version,
    audit_log.rule_pack_version,
    audit_log.created_at
  FROM audit_log
  WHERE audit_log.deal_id IS NOT NULL

  UNION ALL

  SELECT
    audit_log.subject_id AS deal_id,
    audit_log.id AS audit_id,
    audit_log.actor,
    audit_log.action,
    audit_log.subject_type,
    audit_log.subject_id,
    audit_log.details,
    audit_log.state_change,
    audit_log.aegis_version,
    audit_log.rule_pack_version,
    audit_log.created_at
  FROM audit_log
  JOIN documents ON documents.id = audit_log.subject_id
  WHERE audit_log.deal_id IS NULL
    AND audit_log.subject_type = 'document'

  UNION ALL

  SELECT
    documents.id AS deal_id,
    audit_log.id AS audit_id,
    audit_log.actor,
    audit_log.action,
    audit_log.subject_type,
    audit_log.subject_id,
    audit_log.details,
    audit_log.state_change,
    audit_log.aegis_version,
    audit_log.rule_pack_version,
    audit_log.created_at
  FROM audit_log
  JOIN documents ON documents.merchant_id = audit_log.subject_id
  WHERE audit_log.deal_id IS NULL
    AND audit_log.subject_type = 'merchant';
