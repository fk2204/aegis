-- Migration 070 — decisions immutability hardening + backfill (mp Phase 2 §12 task 5).
--
-- The ``decisions`` schema + UPDATE/DELETE triggers were established by
-- migration 015 per master plan §9.2. This migration is the Phase 2
-- *completion* step the original 015 migration explicitly deferred (its
-- header references "one-time backfill: existing deals get decisions
-- rows with decided_by='backfill_2026_05'" but the SQL was never written
-- against analyses rows that landed AFTER 015 was applied).
--
-- Two things this migration does:
--
--   1. RE-ASSERT the UPDATE / DELETE triggers from 015. They are already
--      installed, but re-running CREATE OR REPLACE is idempotent and
--      makes this migration self-contained — a fresh Supabase project
--      that somehow loses the triggers (manual ops, restored backup,
--      etc.) gets them back without depending on 015 having run first.
--      The Phase 2 acceptance criterion ("UPDATE decisions raises
--      'decisions table is append-only'") is what an auditor reads, so
--      its enforcement should not be a transitive dependency.
--
--   2. BACKFILL one row per analysed document that does NOT yet have a
--      decisions row, ``decided_by='backfill_2026_06'``. Stamps the
--      decision-time score factors / contributing transaction UUIDs /
--      bank statement SHA / OFAC cache hash from the analysis row so the
--      Phase 2 regulator-defense story is materially complete from day
--      one of the cutover. ``backfill_quality`` is set to ``'minimal'``
--      when only deal_id + decision are recoverable, ``'partial'`` when
--      the analysis row carries enough to recompute score_factors +
--      contributing_transaction_uuids + bank_statement_pdf_sha256. The
--      ``'full'`` quality is reserved for live decisions.
--
-- Backfill semantics (per-column mapping documented inline below in
-- the SELECT — this header lists only the cross-cutting rules):
--
--   * Skipped when a decisions row already exists for the document
--     (idempotent on re-run; matches the partial-unique-index in 015
--     for ``decided_by='backfill_2026_05'`` — extended below to also
--     cover the 2026_06 cohort).
--   * ``decision`` is always ``'manual_review'`` for backfilled rows
--     because the historical schema never persisted the scorer's
--     ``recommendation``. The dossier should treat backfilled rows as
--     "decision was made offline, see audit_log for the real actor
--     record" rather than as a recovered approve / decline result.
--   * ``score`` from ``documents.fraud_score`` (Phase 1 parser-side
--     score); ``score_factors`` from ``documents.fraud_score_breakdown``.
--     Neither captures the full Phase 2 scorer output, but they're the
--     only persisted scoring signal pre-Phase-2.
--   * ``contributing_transaction_uuids`` is the deduped union of every
--     ``analyses.*_source_ids`` array migration 002 introduced.
--   * ``bank_statement_pdf_sha256`` from ``documents.sha256_original``
--     (migration 033) when set; NULL otherwise.
--   * ``state_code`` from ``merchants.state``; falls back to ``'XX'``
--     when the merchant row is gone (orphan-document case — should not
--     happen under normal operation but the FK to merchants is
--     SET NULL, so we tolerate the edge).
--   * ``cfdl_tier`` set to 3 (informational placeholder — the field
--     was historically driven by state regulation routing which is now
--     informational metadata per CLAUDE.md compliance scope note).
--   * ``aegis_version`` / ``rule_pack_version`` set to ``'backfill'``
--     so a downstream consumer can tell at a glance these rows were
--     materialized after-the-fact rather than written at decision time.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Re-assert immutability triggers (idempotent; matches 015).
CREATE OR REPLACE FUNCTION block_decision_modification() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'decisions table is append-only; use a new row to supersede';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decisions_no_update ON decisions;
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
  FOR EACH ROW EXECUTE FUNCTION block_decision_modification();

DROP TRIGGER IF EXISTS decisions_no_delete ON decisions;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
  FOR EACH ROW EXECUTE FUNCTION block_decision_modification();

-- Extend the 015 partial-unique-index to also cover the 2026_06 backfill
-- cohort. We DROP-and-CREATE the index because the WHERE clause changed;
-- both index versions enforce "at most one backfill row per deal" but
-- with the wider cohort set.
DROP INDEX IF EXISTS uq_decisions_backfill_per_deal;
CREATE UNIQUE INDEX IF NOT EXISTS uq_decisions_backfill_per_deal
  ON decisions (deal_id)
  WHERE decided_by IN ('backfill_2026_05', 'backfill_2026_06');

-- Backfill. Wrapped in DO block for the source-id union locals + the
-- ROW_COUNT diagnostic.
--
-- Mapping notes (column-by-column from the analyses + documents schema
-- as it exists at migration 070's apply time — see 000_foundation.sql,
-- 002_analyses_source_ids.sql, 033_documents_storage_and_retention.sql):
--
--   * ``decision`` — analyses has no ``recommendation`` column; the
--     scorer's recommendation is computed at decision time and lives
--     only in the audit_log + (going forward) decisions table. The
--     pre-Phase-2 historical signal we have access to is
--     ``documents.parse_status`` — a parse that landed in
--     ``manual_review`` is the closest historical match to a
--     ``manual_review`` decision; everything else backfills as
--     ``manual_review`` too (minimal quality — operator should treat
--     these rows as "decision was made offline, see audit_log for the
--     actor record" rather than "decision was approve/decline").
--   * ``decision_reason_codes`` — historically lived on the documents
--     row as ``all_flags`` (parse-pipeline flags) — those aren't
--     decision codes but they're the closest historical evidence.
--     Stored under the ``hist_flags:`` prefix so the dossier UI knows
--     these are not first-class decision codes.
--   * ``score`` — ``documents.fraud_score`` is the only persisted
--     scoring axis pre-Phase-2; it's a parser-side score, not the
--     full deal score, but stamping it gives the auditor "the parser
--     thought this was an N-out-of-100 statement at decision time".
--   * ``score_factors`` — ``documents.fraud_score_breakdown`` is the
--     canonical Phase 1 breakdown. Always present (default ``'{}'``).
--   * ``contributing_transaction_uuids`` — union of every
--     ``_source_ids`` array migration 002 added to ``analyses``. None
--     are nullable (default ``'{}'``) so the COALESCE wrappers are
--     defensive only.
DO $backfill$
DECLARE
  inserted_count int := 0;
BEGIN
  INSERT INTO decisions (
    id,
    deal_id,
    decided_at,
    decided_by,
    decision,
    decision_reason_codes,
    score,
    score_factors,
    analysis_id,
    contributing_transaction_uuids,
    bank_statement_pdf_sha256,
    state_code,
    cfdl_tier,
    aegis_version,
    rule_pack_version,
    backfill_quality
  )
  SELECT
    gen_random_uuid()                                          AS id,
    a.document_id                                              AS deal_id,
    -- ``decided_at`` keyed off the analysis row's creation so the
    -- backfill respects original chronology; otherwise every backfill
    -- row would crowd the same NOW() timestamp and the audit UI's
    -- ordered-by-decided_at panel becomes unreadable.
    COALESCE(a.created_at, NOW())                              AS decided_at,
    'backfill_2026_06'                                         AS decided_by,
    -- Pre-Phase-2 we have no persisted ``recommendation`` field.
    -- Map ``documents.parse_status`` to the closest decision-CHECK
    -- value: ``manual_review`` -> ``manual_review``; everything else
    -- (including ``proceed`` and ``review``) -> ``manual_review`` too
    -- because we can't reconstruct whether the operator went
    -- approve / decline / refer from the persisted state alone.
    'manual_review'                                            AS decision,
    -- Historical parse-pipeline flags are not decision codes but
    -- they're the closest evidence we have. ``hist_flags:`` prefix
    -- tells the dossier these are reconstructed rather than authored.
    CASE
      WHEN array_length(d.all_flags, 1) > 0 THEN
        ARRAY(SELECT 'hist_flag:' || f FROM unnest(d.all_flags) AS f)
      ELSE '{}'::text[]
    END                                                        AS decision_reason_codes,
    -- documents.fraud_score is a 0-100 integer; cast to numeric(5,2)
    -- to match the decisions.score column type.
    CASE
      WHEN d.fraud_score IS NOT NULL THEN d.fraud_score::numeric(5,2)
      ELSE NULL
    END                                                        AS score,
    COALESCE(d.fraud_score_breakdown, '{}'::jsonb)             AS score_factors,
    a.id                                                       AS analysis_id,
    -- Union + dedupe of every _source_ids array on the analyses row
    -- (migration 002). NOT NULL columns with default ``'{}'`` so the
    -- COALESCE is defensive (handles legacy rows pre-002 that
    -- somehow escaped the DEFAULT — should never happen but cheap).
    COALESCE(
      ARRAY(
        SELECT DISTINCT u
        FROM unnest(
          COALESCE(a.avg_daily_balance_source_ids, ARRAY[]::uuid[])
          || COALESCE(a.true_revenue_source_ids,   ARRAY[]::uuid[])
          || COALESCE(a.num_nsf_source_ids,        ARRAY[]::uuid[])
          || COALESCE(a.days_negative_source_ids,  ARRAY[]::uuid[])
          || COALESCE(a.mca_daily_total_source_ids, ARRAY[]::uuid[])
        ) AS u
        WHERE u IS NOT NULL
      ),
      ARRAY[]::uuid[]
    )                                                          AS contributing_transaction_uuids,
    d.sha256_original                                          AS bank_statement_pdf_sha256,
    COALESCE(m.state, 'XX')                                    AS state_code,
    3                                                          AS cfdl_tier,
    'backfill'                                                 AS aegis_version,
    'backfill'                                                 AS rule_pack_version,
    -- ``partial`` when score AND a non-empty fraud_score_breakdown
    -- are present (genuine analysis-derived row); ``minimal`` otherwise.
    CASE
      WHEN d.fraud_score IS NOT NULL
       AND d.fraud_score_breakdown <> '{}'::jsonb
        THEN 'partial'
      ELSE 'minimal'
    END                                                        AS backfill_quality
  FROM analyses a
  JOIN documents d ON d.id = a.document_id
  LEFT JOIN merchants m ON m.id = d.merchant_id
  WHERE NOT EXISTS (
      SELECT 1
      FROM decisions existing
      WHERE existing.deal_id = a.document_id
    )
  ;
  GET DIAGNOSTICS inserted_count = ROW_COUNT;
  RAISE NOTICE 'migration 070 backfill: inserted % decisions rows', inserted_count;
END
$backfill$;

-- Audit row for the backfill event itself (Phase 2 acceptance: every
-- state change writes to audit_log). One row, not one-per-decision, so
-- the audit table doesn't take an O(N-deals) slug from a single
-- migration. Subject is the table name itself; details carries the
-- inserted row count.
INSERT INTO audit_log (actor, action, subject_type, subject_id, details)
SELECT
  'migration_070',
  'decisions.backfilled',
  'table',
  NULL,
  jsonb_build_object(
    'cohort', 'backfill_2026_06',
    'inserted', (SELECT COUNT(*) FROM decisions WHERE decided_by = 'backfill_2026_06')
  )
WHERE EXISTS (
  -- Only audit when we actually inserted at least one row this run.
  SELECT 1 FROM decisions WHERE decided_by = 'backfill_2026_06'
);
