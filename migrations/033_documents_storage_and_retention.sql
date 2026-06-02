-- Migration 033 — documents storage + retention + merchants soft-delete
--
-- PDF retention redesign chunk A — see docs/PDF_RETENTION_DESIGN.md.
--
-- Adds long-term storage metadata to documents and a soft-delete column
-- to merchants. All new columns nullable; backfill is explicitly NOT
-- performed for legacy rows (~30 documents as of 2026-06-01) — they
-- render dossiers without a "View original PDF" link.
--
-- Plumbing-only at this migration. Worker writes (chunk B), view route
-- (chunk C), dossier UI (chunk D), retention sweep cron (chunk E), and
-- operator script --from-storage flag (chunk F) all build on this
-- schema.
--
-- Columns added to documents:
--   storage_path           — Supabase Storage path to the ciphertext blob
--                            (e.g. merchants/{m}/documents/{d}.pdf.enc or
--                             unassigned/documents/{d}.pdf.enc for
--                             orphan docs with merchant_id IS NULL).
--                            NULL = legacy row OR upload step failed.
--   sha256_original        — SHA-256 (hex) of the plaintext PDF computed
--                            at the moment of encryption. Anchor for the
--                            two-layer integrity check at view time
--                            (AES-GCM auth tag + this hash). Should
--                            equal documents.file_hash in practice;
--                            divergence is fail-closed in the worker.
--   encryption_key_version — Which PDF_ENCRYPTION_KEY_V{n} from
--                            /etc/aegis/aegis.env sealed this blob.
--                            Read by decrypt at view + reparse time.
--   retention_until        — Hard-delete deadline. Baseline NOW()+7yr at
--                            upload; extended via GREATEST(...) on
--                            merchant soft-delete to ≥ NOW()+5yr from
--                            the soft-delete moment. Commera internal
--                            policy (NOT 16 CFR §1020.220 CIP — AEGIS
--                            is not a covered financial institution).
--
-- Column added to merchants:
--   deleted_at             — Soft-delete timestamp. NULL = active.
--                            UI/API to set this is deferred; the column
--                            lives here so chunk B's retention extender
--                            has a trigger to hang off.
--
-- Indexes added:
--   idx_documents_retention_until — partial index, only rows that still
--                            have ciphertext to delete. The nightly
--                            retention sweep scans this set.
--   idx_merchants_deleted_at — partial index, only soft-deleted rows.

BEGIN;

ALTER TABLE public.documents
  ADD COLUMN IF NOT EXISTS storage_path           TEXT,
  ADD COLUMN IF NOT EXISTS sha256_original        TEXT,
  ADD COLUMN IF NOT EXISTS encryption_key_version INT,
  ADD COLUMN IF NOT EXISTS retention_until        TIMESTAMPTZ;

COMMENT ON COLUMN public.documents.storage_path IS
  'Supabase Storage path to the ciphertext blob. NULL = legacy row '
  '(pre-chunk-B) OR storage upload failed at parse time (the local '
  'plaintext is preserved in /var/lib/aegis/uploads/quarantine/ for '
  'the reconcile cron to retry).';

COMMENT ON COLUMN public.documents.sha256_original IS
  'SHA-256 (hex) of the plaintext PDF at the moment of encryption. '
  'Verified on every read against decrypt(storage_path) to catch '
  'storage corruption that somehow validated the AES-GCM auth tag.';

COMMENT ON COLUMN public.documents.encryption_key_version IS
  'Identifies which PDF_ENCRYPTION_KEY_V{n} from /etc/aegis/aegis.env '
  'sealed the ciphertext at storage_path. Allows lazy / batch key '
  'rotation without re-encrypting every blob at rotation time.';

COMMENT ON COLUMN public.documents.retention_until IS
  'Hard-delete deadline enforced by the nightly retention sweep cron. '
  'Baseline NOW()+7yr at upload; extended via GREATEST(retention_until, '
  'NOW()+5yr) on merchant soft-delete. Commera internal retention '
  'policy — NOT a 16 CFR §1020.220 CIP requirement.';

CREATE INDEX IF NOT EXISTS idx_documents_retention_until
  ON public.documents (retention_until)
  WHERE storage_path IS NOT NULL;

ALTER TABLE public.merchants
  ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

COMMENT ON COLUMN public.merchants.deleted_at IS
  'Soft-delete timestamp. NULL = active merchant. Setting this from '
  'NULL to NOW() triggers the chunk-B retention extender which raises '
  'retention_until for every storage_path-set document of this '
  'merchant to GREATEST(retention_until, NOW()+5yr).';

CREATE INDEX IF NOT EXISTS idx_merchants_deleted_at
  ON public.merchants (deleted_at)
  WHERE deleted_at IS NOT NULL;

COMMIT;
