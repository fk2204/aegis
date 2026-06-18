-- Migration 066 — merchant_notes table (Feature C — operator notes panel
-- redesign).
--
-- Replaces the legacy single-text-column ``merchants.notes`` append-only
-- string (migration 058) with a properly normalized rows-per-note table.
-- Motivation:
--
--   * The 2026-06-18 dossier redesign moved the operator-notes panel into
--     a full-width block above the chips section with a list of
--     timestamped note cards. Cards need stable IDs so other concurrent
--     workstreams (Agent 1 — note delete) can reference one row without
--     parsing prepended text.
--   * The character-counter + length cap (4000 chars) lives at the
--     application layer; the DB enforces the cap via a CHECK constraint
--     so a SQL-direct insert can't bypass the gate and produce a row that
--     would later 500 the dossier render.
--   * Newest-first display is the only access pattern — index on
--     ``(merchant_id, created_at DESC)`` mirrors the
--     ``merchants_shadow_signals`` precedent (migration 044) for the same
--     "per-merchant render history" query shape.
--
-- The legacy ``merchants.notes`` column (migration 058) stays in place;
-- pre-066 notes accumulated under that column remain readable via SQL.
-- The dossier renderer reads ONLY from this new table going forward —
-- application code in ``aegis.merchants.repository`` exposes
-- ``add_note`` / ``list_notes`` that write/read here.
--
-- PII discipline (per CLAUDE.md): note bodies are operator-curated free
-- text and may contain merchant names / broker context / pricing detail.
-- Loggers MUST mask by key name (``body``) and the audit row paired with
-- each insert carries only the length, NEVER the body bytes themselves.
--
-- Idempotent via ``IF NOT EXISTS``.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS merchant_notes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- The merchant the note is bound to. Soft FK (no REFERENCES) — matches
  -- the migration 044 / 057 precedent so a future merchant soft-delete
  -- doesn't cascade through historical notes.
  merchant_id UUID NOT NULL,

  -- The note body itself. NOT NULL — the application route 400s on an
  -- empty submission before reaching this insert. CHECK enforces the
  -- application-layer 4000-char cap so a SQL-direct bypass would error
  -- at write time instead of producing a row the dossier later chokes
  -- on. The trim_lower length is checked post-trim by the application;
  -- the column stores the trimmed body verbatim so the cap is a true
  -- byte ceiling.
  body TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),

  -- Free-text actor identifier. Convention mirrors migrations 044 /
  -- 057: ``dashboard`` for the dossier-form path (with the operator's
  -- Cloudflare Access email when available). Future SQL-direct edits
  -- use a recognizable label so the operator can spot them in the
  -- dossier history.
  actor TEXT NOT NULL,

  -- When the operator clicked Save. NOT NULL.
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-merchant render history — drives the dossier "Operator notes"
-- card list newest-first. Mirrors the 044 precedent for this exact
-- query shape.
CREATE INDEX IF NOT EXISTS idx_merchant_notes_merchant_created
  ON merchant_notes (merchant_id, created_at DESC);

-- Default-deny RLS: internal-only operator notes, accessible to the
-- service role only. Mirrors migrations 036 / 037 / 040 / 042 / 044 /
-- 057.
ALTER TABLE merchant_notes ENABLE ROW LEVEL SECURITY;
