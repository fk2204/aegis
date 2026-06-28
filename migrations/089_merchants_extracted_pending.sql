-- Migration 089 — staging column for Close Lead description-parsed
-- application data pending operator confirmation.
--
-- Context. Migration 087 added the structured ``FINANCIAL:`` block parser
-- on the ``POST /webhooks/close`` path: when a Close Lead description
-- carries a ``FINANCIAL:`` header followed by Key:Value lines, the
-- pure-string parser at
-- ``aegis.close.field_map._parse_close_lead_description`` populates the
-- ``merchants.stated_*`` columns directly.
--
-- This migration adds the FALLBACK path. Some Close leads do NOT carry a
-- ``FINANCIAL:`` block but DO carry the same application data in a
-- different shape — free-form text, a ``DEAL:`` block, a phone-call
-- transcript, or a partial ``FINANCIAL:`` block with new labels that
-- post-date the static label table. The fallback runs a Bedrock
-- extraction over the full description body and writes the result to
-- THIS column (a single JSONB blob), NOT to the live ``stated_*`` columns.
--
-- Per CLAUDE.md "Extraction & automation assists, never replaces judgment":
-- LLM extraction is a pre-fill assistant with human confirmation, never
-- autonomous creation. The operator confirms via
-- ``/ui/merchants/{id}/extracted-pending/confirm`` (promotes the JSONB
-- payload to ``stated_*``) or discards via
-- ``/ui/merchants/{id}/extracted-pending/discard`` (clears the column).
-- Scoring NEVER reads this column — only the confirmed ``stated_*``
-- columns drive decisions.
--
-- JSONB shape (see ``aegis.close.description_extractor.ExtractedFields``):
--   {
--     "fields": {                       -- AEGIS-side field names
--       "monthly_revenue": "175000.00", -- money as Decimal-safe string
--       "requested_amount": "325000.00",
--       "stated_mca_positions": 2,
--       "stated_daily_payment": "4764.35",
--       "stated_current_lenders": ["Revenued", "IOU"],
--       "stated_bank": "TD Bank",
--       "use_of_funds": "Working capital",
--       ...
--     },
--     "confidences": {                  -- per-field 0.0-1.0 from the LLM
--       "monthly_revenue": 0.92,
--       "requested_amount": 0.97,
--       ...
--     },
--     "extracted_at": "2026-06-28T14:00:00+00:00",
--     "source_chars": 4123,             -- description length seen by LLM
--     "model_id": "us.anthropic.claude-sonnet-4-6"
--   }
--
-- Idempotent: ``ADD COLUMN IF NOT EXISTS`` so a partial re-apply or
-- bootstrap-probe-then-replay leaves the schema in the same end state.

BEGIN;

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS stated_extracted_pending jsonb;

COMMENT ON COLUMN merchants.stated_extracted_pending IS
  'Description-parsed application data awaiting operator confirmation. '
  'Populated by aegis.close.description_extractor when the Close Lead '
  'description has no FINANCIAL block (or the FINANCIAL parser returned '
  'an empty dict). Promoted to stated_* columns via '
  '/ui/merchants/{id}/extracted-pending/confirm or discarded via '
  '/extracted-pending/discard. Never read directly by scoring — only the '
  'confirmed stated_* columns drive decisions.';

COMMIT;
