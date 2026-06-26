-- Migration 075 — analyses.narrator_summary jsonb (Bedrock-driven plain-English summary).
--
-- Adds a single nullable JSONB column to the analyses table to cache the
-- ``NarratorSummary`` shape produced by ``aegis.scoring_v2.narrator``:
--
--   {
--     "deal_summary": str,                     -- 3-5 sentence verbal handoff
--     "flag_explanations": [                   -- one entry per fired flag
--       {
--         "flag_code": "preloan_spike",
--         "severity": "info|warn|decline",
--         "explanation": "..."                 -- THIS deal's actual numbers
--       },
--       ...
--     ],
--     "recommended_action": {
--       "action": "submit_now|call_first|request_documents|do_not_submit",
--       "next_step": "...",                    -- exact next thing to do
--       "top_funder_match": "..." | null,
--       "estimated_terms": "..." | null
--     },
--     "model_id": "us.anthropic.claude-sonnet-4-6",
--     "generated_at": "2026-06-26T12:34:56Z",  -- UTC ISO-8601
--     "version": 1
--   }
--
-- Decimals (none today, but possible in future ``estimated_terms``
-- elaborations) would serialize as strings — same convention as
-- ``monthly_breakdown`` and ``pattern_analysis``. UUIDs none.
--
-- Nullable, no backfill. Existing rows stay NULL — the dossier route
-- opens the raw-analysis ``<details>`` open-by-default when this column
-- is NULL so legacy deals don't lose visibility on the underlying
-- numbers. New analyses get populated on the next dossier render or
-- when the operator clicks "Refresh summary".
--
-- Read by the dossier render at ``GET /ui/merchants/{merchant_id}``
-- and refreshed in place by
-- ``POST /ui/merchants/{merchant_id}/documents/{document_id}/narrator/refresh``.
--
-- Bedrock failure write-posture: when ``narrate_deal`` raises (network
-- blip, model timeout, malformed tool-use envelope), the route leaves
-- this column at its current value (NULL on first attempt, or the
-- previous good summary on a refresh) and surfaces an empty-state hint
-- on the dossier. NEVER write null over a previously-good row — the
-- refresh route enforces that by skipping the UPDATE on failure.
--
-- Source of truth for the underlying deal data stays in
-- ``analyses`` + ``documents`` + ``transactions``. ``narrator_summary``
-- is a presentation cache and may be regenerated freely; the persisted
-- record is purely the most-recent good output.

ALTER TABLE public.analyses
  ADD COLUMN IF NOT EXISTS narrator_summary JSONB;

COMMENT ON COLUMN public.analyses.narrator_summary IS
  'Cached NarratorSummary from aegis.scoring_v2.narrator. NULL on rows
   analyzed before migration 075, or when Bedrock has not yet been
   successfully called for this deal. Populated on dossier render and
   refresh. NOT a source of truth — the underlying score / flags /
   aggregates remain the canonical record.';

-- One audit row for the schema change itself (CLAUDE.md compliance:
-- every state change writes to audit_log).
INSERT INTO audit_log (actor, action, subject_type, subject_id, details)
VALUES (
  'migration_075',
  'analyses.schema_extended',
  'table',
  NULL,
  jsonb_build_object(
    'columns_added', jsonb_build_array('narrator_summary'),
    'nullable', true,
    'backfill', 'none'
  )
);
