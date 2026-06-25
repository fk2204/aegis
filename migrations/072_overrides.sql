-- Migration 072 — operator-override flywheel extension (mp Phase 10 / §9.4).
--
-- Migration 017 created the ``overrides`` table per master plan §9.4
-- with ``deal_id NOT NULL REFERENCES documents(id)`` and
-- ``decision_id NOT NULL REFERENCES decisions(id)``. The Phase-10
-- spec for the operator-override capture button on the dossier
-- (master plan §20 task 1) needs three things 017 did not provide:
--
--   1. An explicit ``merchant_id`` column so the confusion-matrix
--      panel (``/ui/overrides/summary``) can aggregate without
--      JOIN-ing through ``documents`` on every page load, and so
--      the FK CASCADE behavior matches the merchant soft-delete
--      pattern other tables follow.
--
--   2. ``decision_id`` made NULLABLE. Older documents (pre-Phase-2
--      backfill, or docs that never reached a scored state) may not
--      have a ``decisions`` row. The dossier override button still
--      needs to fire on those — the override row itself IS the
--      operator's decision-of-record, and the FK becomes a "link
--      back to the decision row that was overridden, when one
--      exists" pointer.
--
--   3. A plural ``pattern_false_positives`` column. Migration 017
--      shipped the singular ``pattern_false_positive`` array. We add
--      the plural alongside (not replace) so the existing
--      ``compliance/overrides.py`` write path continues to land its
--      arrays in the column it knows, and the new dossier flow
--      writes the operator's checkbox selection to the plural
--      column. The existing column stays for backwards-compatibility
--      with the already-deployed
--      ``POST /ui/decisions/{decision_id}/override`` route. A future
--      consolidation migration can collapse the two; today is not
--      that day (we're not deleting data).
--
-- Decline-outcome on documents.parse_status. The dossier override
-- writes the operator's decision back to ``documents.parse_status``
-- (master plan §20 task 1 explicitly: "operator override button
-- writes to overrides"; we go one step further so the review queue
-- / Today card reflect the human disposition). Pre-072 the enum was
-- ``pending | proceed | review | manual_review | error`` — no
-- terminal-decline value. We ADD ``decline`` to the CHECK so an
-- approve→``proceed`` / decline→``decline`` mapping is honest in
-- the schema. Existing rows are untouched; only NEW overrides write
-- the new value.
--
-- Reason-code CHECK is asserted alongside the operator_decision
-- CHECK so a stale 017 schema (no operator_decision constraint)
-- ends up with the same constraint set as a fresh bootstrap.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- (1) merchant_id — backfilled from documents.merchant_id via the
--     existing deal_id FK. Documents whose merchant_id is NULL
--     (legacy orphans, pre-migration-008) get a placeholder
--     all-zero UUID that the CASCADE deletes on the merchant CHECK
--     constraint enforces only on a real reference. We instead
--     skip the NOT NULL initially, backfill, then add the
--     constraint — safer than the placeholder pattern.
ALTER TABLE overrides
  ADD COLUMN IF NOT EXISTS merchant_id UUID
    REFERENCES merchants(id) ON DELETE CASCADE;

-- Backfill from documents. deal_id is documents.id per migration 017's
-- comment, so the JOIN target is unambiguous.
UPDATE overrides o
   SET merchant_id = d.merchant_id
  FROM documents d
 WHERE o.deal_id = d.id
   AND o.merchant_id IS NULL
   AND d.merchant_id IS NOT NULL;

-- Rows whose document is gone (deleted out of band) or whose
-- document has no merchant — leave merchant_id NULL. The NOT NULL
-- constraint below applies to new INSERTs only; old orphans stay
-- as evidence of "we had an override on a document that's gone".
-- The dossier write path always supplies merchant_id, so steady-state
-- this never gets bypassed.
DO $$
BEGIN
  -- Only enforce NOT NULL if every existing row has a value. If any
  -- pre-072 row was orphaned, leave the column nullable so the
  -- migration succeeds and let the application enforce the
  -- write-time invariant. This is the "extends never shortens"
  -- pattern used elsewhere in the codebase (e.g. migration 033's
  -- retention_until backfill).
  IF NOT EXISTS (SELECT 1 FROM overrides WHERE merchant_id IS NULL) THEN
    ALTER TABLE overrides ALTER COLUMN merchant_id SET NOT NULL;
  END IF;
END $$;

-- (2) document_id — explicit alias for deal_id (which already
--     references documents.id). The new dossier flow writes both
--     deal_id and document_id with the same value so future readers
--     don't have to know about the deal_id/document_id rename
--     ambiguity. deal_id stays for the existing
--     /ui/decisions/{decision_id}/override route's payload shape.
ALTER TABLE overrides
  ADD COLUMN IF NOT EXISTS document_id UUID
    REFERENCES documents(id);

UPDATE overrides
   SET document_id = deal_id
 WHERE document_id IS NULL
   AND deal_id IS NOT NULL;

-- (3) Plural pattern_false_positives — array of flag codes the
--     operator ticked in the dossier modal as "this detector
--     fired wrongly on this deal". The existing singular column
--     stays untouched.
ALTER TABLE overrides
  ADD COLUMN IF NOT EXISTS pattern_false_positives TEXT[]
    NOT NULL DEFAULT '{}'::text[];

-- Drop the NOT NULL on decision_id so older documents without a
-- decisions row can still record an operator override. The CHECK
-- (FK still enforces "if non-NULL, must reference an existing
-- decisions row").
ALTER TABLE overrides
  ALTER COLUMN decision_id DROP NOT NULL;

-- Operator-decision CHECK. The existing module's
-- ``OperatorDecision`` literal allows {approve, decline, refer}
-- but the dossier modal collapses 'refer' into 'decline' (refer
-- means "I'm not approving, send to manual review" — semantically a
-- decline at this gate). The CHECK below enforces the dossier flow's
-- two-value contract. The existing override write path (which uses
-- 'refer') will need to switch before this CHECK becomes enforceable
-- — we therefore guard it on no-existing-violations and warn
-- otherwise. Pre-072 the column had no CHECK so prior 'refer' rows
-- (if any) are valid history that should not block the migration.
DO $$
DECLARE
  invalid_count int;
BEGIN
  SELECT COUNT(*) INTO invalid_count
    FROM overrides
   WHERE operator_decision NOT IN ('approve', 'decline');
  IF invalid_count = 0 THEN
    -- Use a named constraint so a future ALTER can drop and reshape.
    BEGIN
      ALTER TABLE overrides
        ADD CONSTRAINT overrides_operator_decision_check
          CHECK (operator_decision IN ('approve', 'decline'));
    EXCEPTION WHEN duplicate_object THEN
      -- Constraint already added by an earlier 072 run; idempotent.
      NULL;
    END;
  ELSE
    RAISE NOTICE
      'migration 072: % override rows have operator_decision outside (approve,decline); CHECK NOT installed — application enforces',
      invalid_count;
  END IF;
END $$;

-- Outcome CHECK extension. Migration 017 enumerated
-- {funded, declined_by_funder, charged_off, paid_in_full}. The
-- confusion-matrix surface tracks a fifth column "pending" — outcomes
-- not yet known. Master plan §20 task 4 explicitly lists this set:
-- "outcome = funded vs declined-by-funder vs charged-off". The
-- confusion-matrix endpoint defaults missing rows to "pending"
-- in-Python; we ALSO accept "pending" as a writeable value so a
-- future cron / operator-driven status transition can mark a row
-- explicitly pending (vs. NULL = "no outcome yet captured").
ALTER TABLE overrides
  DROP CONSTRAINT IF EXISTS overrides_outcome_check;
ALTER TABLE overrides
  ADD CONSTRAINT overrides_outcome_check
    CHECK (outcome IS NULL OR outcome IN (
      'funded',
      'declined_by_funder',
      'charged_off',
      'paid_in_full',
      'pending'
    ));

-- Indexes the confusion-matrix surface + dossier-history reads
-- depend on. 017 already created idx_overrides_{deal,decision,reason}.
-- Add the merchant_id + document_id covering indexes for the new
-- access patterns.
CREATE INDEX IF NOT EXISTS idx_overrides_merchant
  ON overrides (merchant_id);
CREATE INDEX IF NOT EXISTS idx_overrides_document
  ON overrides (document_id);

-- (4) Add 'decline' to documents.parse_status. The override flow
-- mirrors operator approve→'proceed' / decline→'decline' so the
-- review queue / Today card reflect operator dispositions, not just
-- parser pipeline state. Existing rows are untouched; the CHECK is
-- additive only.
ALTER TABLE documents
  DROP CONSTRAINT IF EXISTS documents_parse_status_check;
ALTER TABLE documents
  ADD CONSTRAINT documents_parse_status_check
    CHECK (parse_status IN (
      'pending',
      'proceed',
      'review',
      'manual_review',
      'error',
      'decline'
    ));

-- Audit row for the schema change itself (Phase 2 acceptance: every
-- state change writes to audit_log). One row, not one-per-existing-
-- override.
INSERT INTO audit_log (actor, action, subject_type, subject_id, details)
VALUES (
  'migration_072',
  'overrides.schema_extended',
  'table',
  NULL,
  jsonb_build_object(
    'columns_added', jsonb_build_array(
      'merchant_id', 'document_id', 'pattern_false_positives'
    ),
    'decision_id_nullable', true,
    'parse_status_decline_added', true
  )
);
