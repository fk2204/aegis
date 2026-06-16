-- Migration 062 — pdf_store: relocate ciphertext blob to Supabase Storage.
--
-- Migration 060 stored AES-GCM ciphertext as BYTEA inline in
-- ``pdf_store.ciphertext``. That worked for the in-Postgres "operator
-- simplification" of the chunk-B retention design, but the 2026-06-16
-- live recovery pass (``scripts/recover_legacy_docs.py --apply``) hit
-- PostgREST request-body rejections on every PDF in the 100 KB–1.2 MB
-- range, six in a row across two merchants. PostgREST's default body
-- ceiling is 1 MB; base64-encoded BYTEA inflates ~33 %, so any sealed
-- blob whose plaintext exceeded ~750 KB blew the wire limit before it
-- ever reached Postgres. Postgres itself handles BYTEA up to ~30 MB,
-- but PostgREST is the gate the supabase-py client routes through, so
-- the limit applied regardless.
--
-- Fix: store ciphertext as a blob in Supabase Storage and keep only
-- the path in ``pdf_store`` — the same shape ``documents.storage_path``
-- already uses for chunk-B's separate ciphertext copy. The view route
-- now reads the path, downloads the blob via the service-role
-- ``storage.from_(bucket).download(path)`` API (NOT a signed URL —
-- ``tests/test_security_invariants.py`` keeps that constraint in
-- place), decrypts, and SHA-verifies as before. BYTEA columns kept
-- nullable for backward compat with rows written before this migration;
-- a CHECK constraint enforces "either storage_path OR (ciphertext +
-- nonce)" so a malformed insert can never produce an undelivable row.
--
-- Path convention: ``pdf_store/{document_id}.pdf.enc``. Distinct from
-- chunk-B's ``merchants/{merchant_id}/documents/{document_id}.pdf.enc``
-- so the two copies are independently auditable; consolidating them
-- is a future plumbing change.
--
-- Schema delta:
--   * ``storage_path TEXT``   — bucket path holding the ciphertext.
--                              NULL for legacy rows (pre-062) that
--                              still carry the blob inline.
--   * ``ciphertext``          — was NOT NULL; now nullable.
--   * ``nonce``               — was NOT NULL; now nullable.
--   * ``pdf_store_blob_or_path`` CHECK constraint — at least one
--                              storage mode must be populated. A row
--                              with neither path nor blob has nothing
--                              to decrypt.
--
-- Backward compatibility: every pre-062 row keeps its inline blob;
-- ``SupabasePdfStoreRepository.fetch_plaintext`` checks ``storage_path``
-- first and falls back to ``ciphertext`` when path is NULL. Once an
-- operator-side sweep migrates legacy rows to Storage paths, the
-- BYTEA columns can be dropped in a follow-up migration.

BEGIN;

ALTER TABLE pdf_store
  ADD COLUMN IF NOT EXISTS storage_path TEXT;

ALTER TABLE pdf_store
  ALTER COLUMN ciphertext DROP NOT NULL;

ALTER TABLE pdf_store
  ALTER COLUMN nonce DROP NOT NULL;

ALTER TABLE pdf_store
  ADD CONSTRAINT pdf_store_blob_or_path
    CHECK (
      storage_path IS NOT NULL
      OR (ciphertext IS NOT NULL AND nonce IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS pdf_store_storage_path_idx
  ON pdf_store (storage_path)
  WHERE storage_path IS NOT NULL;

COMMIT;

-- Verification queries (run separately after apply):
--   SELECT count(*) FROM pdf_store WHERE storage_path IS NOT NULL;
--   SELECT count(*) FROM pdf_store WHERE storage_path IS NULL AND ciphertext IS NOT NULL;
--   SELECT count(*) FROM pdf_store WHERE storage_path IS NULL AND ciphertext IS NULL;  -- must be 0
