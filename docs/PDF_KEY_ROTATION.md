# PDF Encryption Key Rotation

**Companion to** `docs/PDF_RETENTION_DESIGN.md`. Documents the rotation procedure for the AES-256-GCM keys used by the chunk-B worker to encrypt PDFs at rest in Supabase Storage. The runtime support — versioned key lookup, per-document `encryption_key_version`, optional lazy / batch re-encryption — lands with chunks A/B. **The rotation itself is an ops procedure, NOT a code change.**

---

## When to rotate

- **Scheduled:** annually, as a routine hygiene practice. Documented in `deploy/RUNBOOK.md` credential rotation log when executed.
- **Triggered:**
  - Suspected key compromise (operator handed the key to the wrong person; `/etc/aegis/aegis.env` exposed; box snapshot leaked).
  - Personnel change with key access (the operator who pasted V1 into `aegis.env` leaves the company).
  - Cryptographic-library advisory (extreme edge case — AES-256-GCM has held up for 20+ years; rotation here is precautionary, not remediation).

## Rotation procedure

### 1. Generate a new key

On any operator workstation with `openssl`:

```bash
openssl rand -base64 32 > /tmp/aegis-pdf-key-v2.b64
```

The key is exactly 32 bytes raw → 44 characters base64. Verify:

```bash
wc -c < /tmp/aegis-pdf-key-v2.b64   # 45 (includes trailing newline)
base64 -d /tmp/aegis-pdf-key-v2.b64 | wc -c   # 32
```

If either differs, regenerate. The boot guard in `aegis.crypto._decode_key` refuses any key that doesn't decode to exactly 32 bytes.

### 2. Add the new key to the box

SSH into the box as root (key rotation is one of the documented root-SSH ops actions per `.claude/rules/deploy.md`). Append to `/etc/aegis/aegis.env`:

```
PDF_ENCRYPTION_KEY_V2=<contents of /tmp/aegis-pdf-key-v2.b64, no surrounding quotes, no newline>
```

Do NOT change `PDF_ENCRYPTION_KEYS_CURRENT` yet. The new key is configured but not active for writes.

### 3. Verify the boot guard accepts the new key

```bash
sudo systemctl restart aegis-web aegis-worker
sudo journalctl -u aegis-web --since "1 minute ago" -p err --no-pager
```

If the boot guard rejects the key (wrong length, base64 invalid, etc.), the service refuses to start and the journal logs `CryptoConfigError: PDF_ENCRYPTION_KEY_V2 ...`. Fix the env file and retry.

If startup succeeds, the new key is loaded but no document references it yet (`PDF_ENCRYPTION_KEYS_CURRENT` still points at V1).

### 4. Activate the new key for writes

Update `/etc/aegis/aegis.env`:

```
PDF_ENCRYPTION_KEYS_CURRENT=2
```

Restart:

```bash
sudo systemctl restart aegis-web aegis-worker
```

From this point, every new PDF upload is sealed with V2. The boot guard verifies that `PDF_ENCRYPTION_KEYS_CURRENT=2` points at a configured key that decodes to 32 bytes; failure refuses to start.

### 5. Decide on existing-blob re-encryption (optional)

Existing blobs in Supabase Storage are still sealed with V1. The `documents.encryption_key_version` column records which key sealed each blob, so the view route (chunk C) decrypts correctly via the per-document version. **No immediate action is required** — V1 stays valid as long as any row references it.

Two strategies for re-encrypting existing blobs at the operator's pace:

#### 5a. Lazy re-encryption (built-in option, off by default)

On each `GET /api/documents/{id}/original`, if `encryption_key_version < current_key_version()`, the view route re-encrypts the plaintext with the current version and updates the row. Adds ~30ms per view but rotates the corpus organically as documents are accessed.

To enable: set `AEGIS_PDF_LAZY_REENCRYPT=true` in `/etc/aegis/aegis.env` and restart. Off by default to keep view latency predictable.

#### 5b. Batch re-encryption (out of scope for v1; documented for the future)

A future arq job iterates `documents WHERE encryption_key_version < current_key_version() AND storage_path IS NOT NULL`, downloads + decrypts + re-encrypts + uploads each blob, updates the row, audits `document.original_reencrypted`. Run off-hours (3:30 UTC, after the retention sweep at 3:00) to minimize Supabase API contention.

**Not built in v1.** Document the contract here so a future implementer doesn't reinvent it.

### 6. Retire the old key (only after every row references a newer version)

Once `SELECT COUNT(*) FROM documents WHERE encryption_key_version = 1 AND storage_path IS NOT NULL` returns zero, the V1 key can be removed from `/etc/aegis/aegis.env`. Until that point, V1 must stay configured or every legacy doc's view route returns 500 (decrypt fails on missing key).

Log the retirement in `deploy/RUNBOOK.md` under credential rotation log with:
- Date
- Reason for rotation (scheduled / triggered)
- V1 retirement date
- Final V1 row count at retirement (should be 0)

---

## What rotation does NOT do

- It does not improve the threat model against a box compromise. If the box is compromised, both V1 and V2 are exposed (they share `/etc/aegis/aegis.env`). Mitigating box compromise requires KMS or HSM — deferred to a future migration, see `docs/PDF_RETENTION_DESIGN.md` §2.
- It does not re-encrypt blobs automatically. Without lazy or batch re-encryption (§5), existing blobs stay sealed with their original version.
- It does not change `documents.sha256_original`. The plaintext hash is the integrity anchor — independent of which key sealed the ciphertext. A re-encrypted blob has a new ciphertext but the same sha256_original.

---

## Backup posture (informational)

Supabase Storage durability is accepted for v1 (11 nines per their SLA). A future secondary backend (S3, Backblaze B2, etc.) requires NO re-encryption — the ciphertext is identical regardless of where it's stored. The migration shape would be: write to both backends, read from primary with fallback, eventually shift primary. Out of scope for chunk A–F; documented so a future operator knows the design preserves this option without forcing a re-key.
