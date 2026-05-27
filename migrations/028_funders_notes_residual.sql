-- Migration 028 — funders.notes_residual column.
--
-- Splits the existing `notes` field into two: `notes` for operator-
-- authored commentary (added via the review UI; empty after fresh
-- extraction), and `notes_residual` for prose the extractor recognised
-- as relevant but could not slot into a structured field.
--
-- Without this split, residual extraction prose would overload `notes`,
-- and operator-added context would silently merge with auto-extracted
-- text — no clean way to tell who wrote what.
--
-- Backward compatible: empty-string default. Existing funders retain
-- their current `notes` content untouched; `notes_residual` starts
-- empty and populates only via re-extraction with the updated prompt
-- (step F of the UI-redesign chain).

ALTER TABLE funders
  ADD COLUMN IF NOT EXISTS notes_residual TEXT NOT NULL DEFAULT '';
