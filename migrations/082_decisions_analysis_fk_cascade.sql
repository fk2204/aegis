-- Migration 082 — decisions.analysis_id FK → ON DELETE CASCADE.
--
-- The original constraint (migration 015) was ON DELETE RESTRICT, which
-- blocked reparse: when the parse pipeline upserts an ``analyses`` row,
-- Supabase's upsert path internally DELETE+INSERTs, and the RESTRICT FK
-- from ``decisions.analysis_id`` aborts the operation with
-- ``decisions_analysis_id_fkey`` violation. The reparse never completes,
-- the doc stays at its original parse_status, and the queue silently
-- drops the work.
--
-- Switching to CASCADE preserves the immutability contract: a reparse
-- IS a new decision. Deleting the old analyses row cascades to delete
-- the old decisions row(s); ``record_decision`` writes a fresh decisions
-- row after the new parse completes. The audit log still carries the
-- original decision's record via the immutability triggers from
-- migration 070 (those triggers fire on UPDATE / DELETE attempts
-- against ``decisions`` directly — a cascade from analyses is a
-- different code path and is the intended semantic for the reparse
-- case).
--
-- Reference incident (2026-06-27 21:53 UTC): bulk reparse of 28 sealed
-- manual_review docs surfaced the failure on docs that already had a
-- decisions row; ~3-5 of the 28 errored with
-- ``decisions_analysis_id_fkey`` while the rest re-parsed cleanly.
-- This migration eliminates that edge case.

BEGIN;

ALTER TABLE decisions
  DROP CONSTRAINT decisions_analysis_id_fkey,
  ADD CONSTRAINT decisions_analysis_id_fkey
    FOREIGN KEY (analysis_id) REFERENCES analyses(id) ON DELETE CASCADE;

COMMIT;
