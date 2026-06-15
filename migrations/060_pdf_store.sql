-- Migration 060 — pdf_store table for in-Postgres ciphertext blobs.
--
-- DEVIATION NOTE (2026-06-15 operator directive):
--   ``docs/PDF_RETENTION_DESIGN.md`` specs Supabase Storage as the
--   long-term ciphertext location and the existing chunk-B worker step
--   (``aegis.workers._try_encrypted_storage_step``) writes there. The
--   2026-06-15 operator prompt SIMPLIFIES that design: encrypted blob +
--   nonce + key_version land in a new ``pdf_store`` table in Postgres
--   instead of Supabase Storage. This migration backs that table.
--
--   The view route (chunk C — ``GET /ui/merchants/{merchant_id}/documents/
--   {document_id}/pdf``) reads from this table, decrypts via
--   ``aegis.crypto.decrypt_pdf``, verifies the plaintext SHA-256 against
--   the row's ``sha256_plaintext`` column, and streams the bytes back to
--   the operator. The route never returns a Supabase signed URL — the
--   security-invariant test in ``tests/test_security_invariants.py``
--   greps source for ``create_signed_url`` / ``get_public_url`` and
--   fails CI if either appears.
--
--   The legacy Supabase Storage path stays in place for now — this table
--   is additive. The worker step that writes here runs AFTER the
--   successful parse + analyses persistence (so a Postgres-side store
--   failure does not erase a valid parse) and BEFORE the local PDF
--   unlink (so a store failure preserves the plaintext for the operator
--   to investigate; see the worker's quarantine helper).
--
-- Schema:
--   * ``document_id``        PK + FK to documents(id) ON DELETE CASCADE.
--                            Cascade matches the documents row lifetime
--                            — a hard delete of a documents row erases
--                            the ciphertext too. Soft deletes (the
--                            retention sweep path) clear the storage
--                            metadata columns on documents without
--                            dropping the row, so cascade is harmless
--                            for the retention case.
--   * ``ciphertext``         AES-GCM sealed bytes from
--                            ``aegis.crypto.encrypt_pdf`` (nonce||ct||tag).
--                            Stored as BYTEA — Postgres handles ~30 MB
--                            blobs comfortably; PDFs are size-capped at
--                            25 MB at upload.
--   * ``nonce``              12-byte AES-GCM nonce. Already embedded in
--                            ``ciphertext[:12]`` (see ``encrypt_pdf``),
--                            but split out here so a future operator
--                            audit query can inspect nonces directly
--                            without parsing the ciphertext.
--   * ``key_version``        Which ``PDF_ENCRYPTION_KEY_V{n}`` sealed
--                            this row. Required for rotation: the
--                            decrypt path looks up the matching key
--                            from ``/etc/aegis/aegis.env``.
--   * ``sha256_plaintext``   Plaintext SHA-256 hex digest. Verified
--                            after every decrypt as belt-and-suspenders
--                            over AES-GCM's auth tag (per CLAUDE.md PDF
--                            retention rule: integrity check on every
--                            read). Mismatch is a hard 500 + audit row.
--   * ``byte_size_plaintext`` Plaintext length in bytes. Surfaces in
--                            audit rows ("we streamed 1.2 MB to this
--                            operator at 14:03Z") without ever logging
--                            the bytes themselves.
--   * ``stored_at``          When this row landed. Used for ops
--                            housekeeping (orphan detection: rows whose
--                            ``documents.storage_path`` was never set).
--
-- search_path is pinned to match the migration 030 hardening pass on
-- functions; nothing here defines a function, but the convention is
-- consistent with the rest of the AEGIS migration corpus.

BEGIN;

CREATE TABLE IF NOT EXISTS pdf_store (
  document_id UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  ciphertext BYTEA NOT NULL,
  nonce BYTEA NOT NULL CHECK (octet_length(nonce) = 12),
  key_version INT NOT NULL CHECK (key_version > 0),
  sha256_plaintext TEXT NOT NULL CHECK (length(sha256_plaintext) = 64),
  byte_size_plaintext INT NOT NULL CHECK (byte_size_plaintext > 0),
  stored_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS pdf_store_stored_at_idx
  ON pdf_store (stored_at DESC);

-- Match migrations 011 / 013 / 057: service_role bypasses RLS; anon /
-- authenticated are denied (no policies = full deny for PostgREST). The
-- view route reads via service_role; no end-user has a direct read path.
ALTER TABLE pdf_store ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT count(*) FROM pdf_store;
--   SELECT key_version, count(*) FROM pdf_store GROUP BY key_version;
--   SELECT count(*) FROM pdf_store
--     WHERE stored_at > NOW() - INTERVAL '30 days';
