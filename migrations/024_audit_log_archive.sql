-- Migration 024 — audit_log_archive cold-storage table (mp Phase 7 §17).
--
-- Archive destination for the retention cron in src/aegis/audit_archiver.py.
-- Schema is a superset of audit_log (every column the live table has, plus
-- archive metadata). The cron INSERTs into the archive THEN deletes from
-- the live table in a single transaction so a partial archive is impossible.
--
-- Why archive instead of delete (master plan §17 "Watch out"):
--   A regulator asking for a record we already deleted is worse than
--   keeping the record. The operator manually deletes from the archive
--   only after an explicit review.
--
-- Schema parity:
--   * Mirrors every column added by 000_foundation, 019_audit_log_extend,
--     and 022_operators_and_roles.
--   * archived_at, archived_by, archive_reason, retention_policy_state and
--     retention_policy_years are NEW — written by the archiver.
--   * source_id is the audit_log.id at archive time (audit_log uses UUID
--     PKs from migration 000). Lets a future re-imported regulator pull
--     reconstruct the original row.
--
-- Idempotency:
--   * source_id is UNIQUE — re-running the cron over the same window
--     fails the second INSERT instead of double-archiving.
--   * The cron handles the conflict by skipping (ON CONFLICT DO NOTHING)
--     so a repeated run produces zero new rows + no audit entry.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS audit_log_archive (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Original audit_log columns (mirrored 1:1).
  source_id UUID NOT NULL UNIQUE,
  actor TEXT NOT NULL,
  actor_email TEXT,
  action TEXT NOT NULL,
  subject_type TEXT,
  subject_id UUID,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_created_at TIMESTAMPTZ NOT NULL,
  deal_id UUID,
  state_change JSONB,
  aegis_version TEXT,
  rule_pack_version TEXT,

  -- Archive metadata.
  archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  archived_by TEXT NOT NULL DEFAULT 'audit_archiver',
  archive_reason TEXT NOT NULL DEFAULT 'retention_expired',
  retention_policy_state TEXT,
  retention_policy_years INTEGER,
  archive_batch_id UUID NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_archive_source ON audit_log_archive (source_id);
CREATE INDEX IF NOT EXISTS idx_audit_archive_archived_at ON audit_log_archive (archived_at);
CREATE INDEX IF NOT EXISTS idx_audit_archive_batch ON audit_log_archive (archive_batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_archive_deal
  ON audit_log_archive (deal_id)
  WHERE deal_id IS NOT NULL;

ALTER TABLE audit_log_archive ENABLE ROW LEVEL SECURITY;
