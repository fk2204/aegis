-- Migration 077 — notifications table for the operator bell-icon
-- dropdown that lands in the topstrip.
--
-- Why we need a dedicated table (vs reusing audit_log)
-- ----------------------------------------------------
-- audit_log is append-only and globally ordered; notifications are
-- per-operator, mutable (read_at flips false→true exactly once), and
-- the bell counts only unread rows. Reusing audit_log would force every
-- bell-unread query through a multi-condition WHERE on a hot table and
-- couple two unrelated concerns at the schema level. Separate table
-- keeps the bell query a tight indexed lookup and the audit_log
-- semantics pristine.
--
-- Event types
-- -----------
-- Only two emitters land here today:
--   * ``merchant_created`` — Close webhook receives a new lead and the
--     parse-side merchant gets upserted. Recipients: every admin (no
--     assignment exists yet at create time).
--   * ``parse_complete`` — worker finishes a parse pass. Recipients:
--     the merchant's current assignee, OR every admin when unassigned
--     (don't drop the notification on the floor).
-- The CHECK constraint guards against typos. New types land via
-- migration when they're added.

BEGIN;

CREATE TABLE IF NOT EXISTS notifications (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recipient_operator_id UUID NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
  event_type            TEXT NOT NULL
    CHECK (event_type IN ('merchant_created', 'parse_complete')),
  payload               JSONB NOT NULL DEFAULT '{}'::jsonb,
  link_url              TEXT,
  read_at               TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index that powers the unread-count badge + the dropdown query.
-- Partial-on-NULL keeps the index small (most rows are read within
-- minutes of creation and stop being interesting).
CREATE INDEX IF NOT EXISTS idx_notifications_recipient_unread
  ON notifications (recipient_operator_id, created_at DESC)
  WHERE read_at IS NULL;

-- Full-recipient index for the dropdown when the operator clicks
-- "show all" (future). Cheap to maintain because the table is
-- per-operator small.
CREATE INDEX IF NOT EXISTS idx_notifications_recipient_created
  ON notifications (recipient_operator_id, created_at DESC);

COMMIT;
