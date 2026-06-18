-- Migration 065 — merchants.deleted_at: operator-initiated soft-delete.
--
-- Adds a nullable ``deleted_at TIMESTAMPTZ`` column to the merchants
-- table so an operator can remove a merchant from the dossier surface
-- without erasing the underlying row (or its descendant documents /
-- transactions / analyses / decisions / audit rows). The row keeps its
-- full history forever; the column only changes whether the merchant
-- appears in operator-visible lists.
--
-- Read contract (application side, NOT enforced by the DB):
--   * Every merchant read path in ``aegis.merchants.repository`` filters
--     out rows where ``deleted_at IS NOT NULL`` — ``get``, ``list_all``,
--     ``find_by_close_lead_id``, ``find_by_close_opportunity_id``,
--     ``find_by_email``, ``count_total``.
--   * ``get(merchant_id)`` on a soft-deleted row raises
--     ``MerchantNotFoundError`` so the existing 404 paths on the
--     dossier, edit form, match panel, etc. surface a normal
--     not-found page instead of rendering against a tombstone.
--   * ``soft_delete`` is the only write path that sets the column. It
--     is filtered on ``deleted_at IS NULL`` so a double-delete raises
--     ``MerchantNotFoundError`` (no silent re-stamp of the timestamp).
--
-- Write surface: ``POST /ui/merchants/{merchant_id}/delete`` on the
-- dossier. Writes one ``audit_log`` row with ``action='merchant.deleted'``
-- before redirecting to the merchants list. Audit-write failure
-- propagates per CLAUDE.md auditability rule.
--
-- Index rationale: the dominant query against this column is
-- ``WHERE deleted_at IS NULL`` (the list path runs on every dashboard
-- load). A partial index keyed on the NULL set keeps that query plan
-- index-only and avoids dragging the (rare) soft-deleted rows into the
-- leaf pages.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS merchants_deleted_at_idx
  ON merchants (deleted_at)
  WHERE deleted_at IS NULL;

COMMENT ON COLUMN merchants.deleted_at IS
  'Operator soft-delete timestamp. NULL = active. NOT NULL = hidden '
  'from every operator-visible list / dossier read; underlying row + '
  'children preserved forever for audit. Set via '
  'POST /ui/merchants/{id}/delete.';

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT count(*) FILTER (WHERE deleted_at IS NULL)   AS active,
--          count(*) FILTER (WHERE deleted_at IS NOT NULL) AS soft_deleted
--     FROM merchants;
--   \d+ merchants
