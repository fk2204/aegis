-- Migration 029 — funders.operator_notes column.
--
-- Adds a dedicated operator-authored commentary field, separate from
-- `notes_residual` (extractor-authored prose the LLM couldn't slot
-- into a structured field) and the legacy `notes` column (which is
-- being phased out — extract.py and the funder_reextract route
-- already migrate non-empty `notes` into `notes_residual` before
-- saving).
--
-- Re-extract preserves operator_notes by NEVER touching it during the
-- merge — same pattern that preserves contact fields when extraction
-- returns empty for them. So an operator who types "Erik prefers ACH
-- on funding day, not lockbox" into the funder page keeps that note
-- even after the operator re-uploads an updated criteria PDF.
--
-- Backward compatible: empty-string default. Existing funder rows
-- start with operator_notes='' and only populate via the new
-- /ui/funders/{id}/operator-notes route or the future edit form
-- (Issue 2).

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS operator_notes TEXT NOT NULL DEFAULT '';
