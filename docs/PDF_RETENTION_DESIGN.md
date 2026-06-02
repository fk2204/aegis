# PDF Retention Redesign — Design Doc

**Status:** Approved by operator 2026-06-01 with five required changes (two blockers) folded in. Awaiting chunk A authorization.

**Supersedes:** the day-one rule "Never store PDFs long-term. Parse → extract → delete from disk in a `finally` block." (CLAUDE.md, replaced when chunk A ships.)

**Owner:** Operator (Filip Kozina). Implementer: Claude.

---

## 1. Why this exists

Day-one design hard-deletes the original PDF in the worker's `finally` block on every code path. Operational cost has grown: visual re-inspection is impossible, parser-improvement re-parse depends on the operator still having a local copy, regulator records-request answers are partial (data only, never the original artifact), funder-forward must come from Close. Storing PDFs long-term in plaintext on the production box is the easy wrong answer; this doc describes the right answer.

**Goal:** retain the original PDF as long as policy requires, but in a form where a compromise of Supabase Storage alone reveals only ciphertext, where every operator access is audited, and where deletion at retention end is provable to a regulator.

**Non-goals for v1:** AWS KMS integration, secondary backup backend, lazy re-encryption job, batch re-encryption, public ACL beyond "any commerafunding.com SSO user". All documented for the future; none built now.

---

## 2. Threat model

**Protected against:**

| Adversary | Capability | Mitigation |
|---|---|---|
| Internet attacker | Direct TCP to box | FastAPI origin bound to `127.0.0.1:5555` (verified — see §3); no inbound port reachable bypassing Cloudflare. |
| Internet attacker via CF | Reaches `/api/documents/{id}/original` | Cloudflare Access SSO gate. SSO + ACL domain check + audit row. |
| Supabase compromise (read of bucket) | Reads every ciphertext blob | AES-256-GCM client-side encryption with key in `/etc/aegis/aegis.env`. Storage compromise → ciphertext only. |
| Supabase compromise (write to bucket) | Substitutes or corrupts a blob | Two-layer integrity check at read time: AES-GCM auth tag + SHA-256 of plaintext vs `documents.sha256_original`. Mismatch → 500 + audit `integrity_failed`. |
| Forged `Cf-Access-Authenticated-User-Email` header from outside the tunnel | Pretends to be an operator | Header cannot reach origin from outside (loopback bind). PLUS new shared-secret header `Cf-Aegis-Tunnel-Secret` verified on every CF-authenticated route (defense-in-depth). |

**NOT protected against (honest threat-model wording — change Q3):**

| Adversary | Capability | Why we accept |
|---|---|---|
| Box compromise (e.g. SSH key theft → root on the box) | Reads both `/etc/aegis/aegis.env` (encryption keys + Supabase service_role creds) AND uses them to decrypt every blob | The encryption key and the storage credentials share `/etc/aegis/aegis.env`. The "ciphertext only" guarantee covers a Supabase-side breach, not a box-side breach. Mitigating this would require KMS (key never on disk) or HSM — deferred to a future migration. |
| Box-local user (operator with sudo, attacker post-SSH-compromise) | `curl http://127.0.0.1:5555/...` with a forged `Cf-Access-Authenticated-User-Email` header and a stolen `Cf-Aegis-Tunnel-Secret` from `/etc/aegis/aegis.env` | The shared-secret header defends against forged headers from network-adjacent attackers (e.g. someone who got code execution as the `aegis` user but no read of `/etc/aegis/aegis.env`). It does not defend against a root-on-box attacker. |

Documenting the boundary explicitly so future code review never silently weakens it.

---

## 3. BLOCKER 1 — Origin lockdown verification (the prerequisite to chunk C)

Verified 2026-06-01 against the production Hetzner box (HEAD `763eac9`):

| Check | Status | Evidence |
|---|---|---|
| Uvicorn binds to `127.0.0.1`, not `0.0.0.0` | ✅ PASS | `deploy/aegis-web.service:19`: `ExecStart=/usr/local/bin/uv run uvicorn aegis.api.app:app --host 127.0.0.1 --port 5555`. Runtime: `ss -tlnp` shows `LISTEN 0 2048 127.0.0.1:5555 0.0.0.0:* users:(("uvicorn",pid=535205,fd=15))`. The kernel rejects external TCP to port 5555. |
| Cloudflare Tunnel is outbound-only | ✅ PASS by design | `cloudflared` dials FROM the box TO Cloudflare. No inbound port for the tunnel. |
| Port 22 (SSH) is the only public listener | ✅ PASS | Same `ss` run shows only `:22` on `0.0.0.0` and `[::]`. SSH is key-only (no password auth per `deploy/install.sh` setup). |
| `ufw` denies inbound except SSH | ⚠️ NOT VERIFIED in this run (sudo password prompt blocked the check) | Added to chunk-A acceptance: SSH as root, run `ufw status verbose`, confirm `5555/tcp` is not in the `ALLOW` list and default-deny is the policy. |

**Shared-secret header (defense-in-depth, added per BLOCKER 1):**

- New env var: `AEGIS_TUNNEL_SHARED_SECRET=<base64-random-32-bytes>`
- New `cloudflared` ingress config injects header: `Cf-Aegis-Tunnel-Secret: <value>` on every request before forwarding to localhost:5555
- New FastAPI dependency `require_tunnel_secret` composed onto every CF-authenticated route (the existing SSO routes AND the new `/api/documents/{id}/original`). Returns 403 if header missing or mismatched. Constant-time compare.
- Header secret lives in `/etc/aegis/aegis.env`. Rotation procedure: generate new secret → update cloudflared config → update aegis.env → restart both. Documented in `deploy/RUNBOOK.md`.

This defends against an attacker who gets code execution as the `aegis` user but not `root` (`/etc/aegis/aegis.env` is `0640 root:aegis`-readable per current install but the shared-secret-only world doesn't help here — flagged in §2 honesty section).

**Authenticated Origin Pull (AOP):** not applicable to a `cloudflared` tunnel setup. AOP is for the case where CF reaches your origin over TLS and you want to verify CF is the caller via mTLS. Our tunnel is outbound HTTP-over-WebSocket from the box; there is no inbound TLS handshake. The tunnel itself authenticates with CF (the tunnel's `credentials.json`). Documented and dismissed.

**Acceptance gate for chunk C:** all four checks above must be ✅ at the moment chunk C deploys. Chunk A and chunk B can proceed without — they don't expose any new external surface.

---

## 4. Architecture overview

```
                                    ┌──────────────────────────────────────┐
                                    │  /etc/aegis/aegis.env                │
                                    │   PDF_ENCRYPTION_KEY_V1 (32b b64)    │
                                    │   PDF_ENCRYPTION_KEYS_CURRENT=1      │
                                    │   AEGIS_TUNNEL_SHARED_SECRET=...     │
                                    │   AEGIS_DOCUMENT_BUCKET=documents    │
                                    │   SUPABASE_SERVICE_KEY=...           │
                                    └──────────────┬───────────────────────┘
                                                   │ loaded at boot
                                                   ▼
                                       ┌──────────────────────┐
                                       │ aegis.crypto         │
                                       │  encrypt_pdf(...)    │
                                       │  decrypt_pdf(...)    │
                                       │  current_key_version │
                                       │  (AES-256-GCM)       │
                                       └──────────┬───────────┘
                                                  │
   [worker.parse_document]                        │
        ↓                                         │
   1. extract+classify+aggregate                  │
   2. persist_parse_result(...)                   │
                                                  │
   ─── NEW chunk B (try block; finally NOT entered yet) ───
   3. plaintext = read(pdf_path)                  │
   4. sha256_original = sha256(plaintext)         │
   5. assert sha256_original == doc.file_hash  ◄──┼── CHANGE 3 (divergence = fail closed)
   6. ciphertext = encrypt_pdf(plaintext, current)│
   7. storage_objects.upload(path, ciphertext)  ◄─┼── raises on non-2xx
   8. persist_storage_metadata(...)  ATOMIC       │   one UPDATE: storage_path,
                                                  │   sha256_original,
                                                  │   encryption_key_version,
                                                  │   retention_until
                                                  │
   9. audit document.original_stored              │
                                                  ▼
                                      ┌────────────────────────┐
                                      │ Supabase Storage       │
                                      │ bucket: documents      │
                                      │ (PRIVATE, service_role │
                                      │  only — checked at     │
                                      │  startup)              │
                                      │ path: merchants/{m}/   │
                                      │       documents/{d}.pdf.enc
                                      │   or unassigned/...    │
                                      └────────────────────────┘

   10. if all 9 steps succeeded:
         _safe_unlink(pdf_path)   ◄── BLOCKER 2: unlink ONLY on success
       else (any exception or assertion above):
         audit document.original_storage_failed
         PRESERVE the local file (no unlink)
         move to /var/lib/aegis/uploads-quarantine/{document_id}.pdf
         schedule reconcile_storage_uploads cron to retry

   ─── existing finally block REMOVED at the top level ───
   (defensive cleanup only fires when storage step is skipped entirely,
   e.g. ambiguous_processor or unknown_document — those paths still
   delete because no storage upload was attempted)

   [Browser dossier — chunk D]
        ↓
   GET /ui/merchants/{id}
   renders per-doc row with conditional:
     {% if doc.storage_path %}
       <a href="/api/documents/{{ doc.id }}/original"
          target="_blank" rel="noopener">View original PDF ↗</a>
     {% endif %}
        ↓
   [NEW route — chunk C]
   GET /api/documents/{id}/original
   1. require_tunnel_secret  (BLOCKER 1: shared-secret header)
   2. require_sso_user_email (CF-Access-Authenticated-User-Email)
   3. ACL: email.endswith("@commerafunding.com")
   4. Load doc; 404 if missing or storage_path NULL
   5. ciphertext = storage_objects.download(doc.storage_path)
   6. plaintext = decrypt_pdf(ciphertext, doc.encryption_key_version)
        (raises InvalidTag → 500 + audit integrity_failed)
   7. sha256(plaintext) == doc.sha256_original
        (mismatch → 500 + audit integrity_failed)
   8. audit document.original_viewed (email, ip, ua, key_version)
   9. StreamingResponse(plaintext, application/pdf, inline,
        Cache-Control: private, no-store)

   [Nightly retention sweep — chunk E]
   arq cron at hour=3, minute=0
   FOR EACH expired doc:
     1. storage_objects.delete(storage_path)
     2. storage_objects.confirm_absent(storage_path)   ◄── CHANGE 5
          (HEAD/list; tolerate 404; raise on anything else)
     3. BEGIN TRANSACTION
          UPDATE documents SET storage_path = NULL WHERE id = ?
          INSERT INTO audit_log (action, details, ...)
            details JSON includes deletion_confirmed: true
        COMMIT
     4. If any step 1-3 fails:
          audit document.retention_delete_failed
          storage_path stays set, next sweep retries

   [Soft-delete retention extension — chunk B / sub-step]
   When merchants.deleted_at goes NULL → NOW():
     UPDATE documents SET
       retention_until = GREATEST(
         retention_until,
         NOW() + INTERVAL '5 years'
       )
     WHERE merchant_id = X AND storage_path IS NOT NULL
     RETURNING id, OLD.retention_until, NEW.retention_until;

     FOR EACH returned row:                    ◄── CHANGE 4(a)
       audit document.retention_extended (
         old_retention_until, new_retention_until,
         triggered_by: "merchant_soft_delete",
         merchant_id
       )

   [Reparse from storage — chunk F]
   scripts/_reparse_one.py --from-storage --document-id X
     1. Load doc; require storage_path != NULL
     2. ciphertext = storage_objects.download(doc.storage_path)
     3. plaintext = decrypt_pdf(ciphertext, doc.encryption_key_version)
     4. assert sha256(plaintext) == doc.sha256_original
     5. audit document.original_reparsed_from_storage  ◄── CHANGE 4(b)
     6. Write to a temp path; enqueue parse_document; same wipe-and-go contract as today
```

---

## 5. Schema — migration 033

```sql
-- migrations/033_documents_storage_and_retention.sql
--
-- PDF retention redesign — adds long-term storage metadata to documents
-- and a soft-delete column to merchants. All new columns nullable;
-- backfill is explicitly NOT performed for legacy rows (~30 docs).

BEGIN;

-- documents: storage metadata + retention clock
ALTER TABLE documents
  ADD COLUMN storage_path           TEXT,
  ADD COLUMN sha256_original        TEXT,
  ADD COLUMN encryption_key_version INT,
  ADD COLUMN retention_until        TIMESTAMPTZ;

-- Partial index — the retention sweep only scans rows that still
-- have ciphertext to delete. Skips legacy (NULL storage_path) and
-- post-sweep (NULL storage_path) rows.
CREATE INDEX idx_documents_retention_until
  ON documents (retention_until)
  WHERE storage_path IS NOT NULL;

-- merchants: soft-delete column (Q1 = Option A)
ALTER TABLE merchants
  ADD COLUMN deleted_at TIMESTAMPTZ;

CREATE INDEX idx_merchants_deleted_at
  ON merchants (deleted_at)
  WHERE deleted_at IS NOT NULL;

-- schema_migrations entry written by the runner; nothing here

COMMIT;
```

**Migration probes** (in `scripts/db_checks/`, registered in `MIGRATION_PROBES`):

| Probe | Asserts |
|---|---|
| `migration-033-columns-exist.sql` | All four document columns + merchants.deleted_at present with correct types |
| `migration-033-indexes-exist.sql` | `idx_documents_retention_until` and `idx_merchants_deleted_at` exist with the expected partial-index predicate |
| `migration-033-no-retained-forever-anomaly.sql` | `SELECT COUNT(*) FROM documents WHERE storage_path IS NOT NULL AND retention_until IS NULL` — must return 0. Catches the "stored ciphertext but no expiry" bug shape. (Smaller fix per operator.) |

Run `db_verify --target prod --check migration-033-no-retained-forever-anomaly` after each deploy that touches the worker.

---

## 6. Encryption module — `src/aegis/crypto.py` (new)

```python
"""Client-side AES-256-GCM encryption for at-rest PDF storage.

Keys live in /etc/aegis/aegis.env as PDF_ENCRYPTION_KEY_V{n} (base64,
exactly 32 bytes each after decode). PDF_ENCRYPTION_KEYS_CURRENT
points at the version used for new writes. Old versions stay in the
env file as long as any documents row still references them.

Compromised Supabase Storage → ciphertext only.
Compromised box → keys + Supabase creds + ciphertext (see threat model
in docs/PDF_RETENTION_DESIGN.md §2).
"""
from __future__ import annotations

import base64
import os
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aegis.config import get_settings

_NONCE_BYTES: Final[int] = 12       # AES-GCM standard
_TAG_BYTES: Final[int] = 16         # AES-GCM standard
_MIN_BLOB_BYTES: Final[int] = _NONCE_BYTES + _TAG_BYTES  # 28


class CryptoConfigError(RuntimeError):
    """Boot-time config problem. Refuses to proceed."""


class CorruptCiphertextError(RuntimeError):
    """Blob shorter than nonce+tag, or AES-GCM auth tag rejected."""


def _decode_key(b64: str, version: int) -> bytes:
    raw = base64.b64decode(b64, validate=True)
    if len(raw) != 32:
        raise CryptoConfigError(
            f"PDF_ENCRYPTION_KEY_V{version} must decode to exactly 32 bytes"
            f" (got {len(raw)})"
        )
    return raw


def _key_for_version(version: int) -> bytes:
    """Look up the raw 32-byte key for the given version."""
    s = get_settings()
    env_name = f"pdf_encryption_key_v{version}"
    secret = getattr(s, env_name, None)
    if secret is None:
        raise CryptoConfigError(
            f"PDF_ENCRYPTION_KEY_V{version} not configured"
        )
    return _decode_key(secret.get_secret_value(), version)


def current_key_version() -> int:
    """The version new writes use. Validated at boot to point at a key
    that decodes to 32 bytes."""
    return get_settings().pdf_encryption_keys_current


def encrypt_pdf(plaintext: bytes, *, key_version: int) -> bytes:
    """Encrypt plaintext with the named key version. Returns
    nonce(12) || ciphertext || tag(16). Per-encryption nonce from
    os.urandom — same plaintext under same key produces different
    ciphertext."""
    key = _key_for_version(key_version)
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, associated_data=None)
    return nonce + sealed


def decrypt_pdf(blob: bytes, *, key_version: int) -> bytes:
    """Inverse of encrypt_pdf. Raises CorruptCiphertextError on any
    integrity failure: blob too short, wrong key, tampered bytes."""
    if len(blob) < _MIN_BLOB_BYTES:
        raise CorruptCiphertextError(
            f"blob shorter than nonce+tag ({_MIN_BLOB_BYTES} bytes); got"
            f" {len(blob)}"
        )
    key = _key_for_version(key_version)
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data=None)
    except Exception as exc:  # InvalidTag from cryptography
        raise CorruptCiphertextError(str(exc)) from exc


__all__ = [
    "CorruptCiphertextError",
    "CryptoConfigError",
    "current_key_version",
    "decrypt_pdf",
    "encrypt_pdf",
]
```

**Boot guard** (added to `aegis.config.Settings` validator): `pdf_encryption_keys_current` must point at a defined `PDF_ENCRYPTION_KEY_V{n}` AND that key must decode to exactly 32 bytes. Failure raises `CryptoConfigError` at first import, refusing to boot.

---

## 7. Storage objects module — `src/aegis/storage_objects.py` (new)

Thin wrapper around the Supabase Storage client. Always raises on non-2xx (precondition for the worker's failure-handling per BLOCKER 2).

```python
"""Supabase Storage helper — opaque blob upload / download / delete
for the encrypted-PDF persistence path.

All operations raise StorageError on any non-2xx. No retries internal
to this module (the caller decides whether to retry, audit, or
quarantine).
"""
from __future__ import annotations

from typing import Protocol

from aegis.config import get_settings


class StorageError(RuntimeError):
    """Wraps non-2xx responses from Supabase Storage."""


class _StorageBackend(Protocol):
    def upload(self, path: str, data: bytes) -> None: ...
    def download(self, path: str) -> bytes: ...
    def delete(self, path: str) -> None: ...
    def confirm_absent(self, path: str) -> bool: ...
    def assert_bucket_private(self) -> None: ...


def assert_bucket_private_at_startup() -> None:
    """Called by app.lifespan. Refuses to boot if the configured
    bucket is public OR has any non-service_role policy that would
    expose ciphertext."""
    backend = _get_backend()
    backend.assert_bucket_private()


def upload(path: str, data: bytes) -> None:
    """Upload ciphertext. Raises StorageError on non-2xx."""
    _get_backend().upload(path, data)


def download(path: str) -> bytes:
    """Fetch ciphertext. Raises StorageError on non-2xx (including 404)."""
    return _get_backend().download(path)


def delete(path: str) -> None:
    """Idempotent — tolerates 404 (the blob was already gone).
    Raises StorageError on any other non-2xx."""
    _get_backend().delete(path)


def confirm_absent(path: str) -> bool:
    """HEAD/list check after delete. Returns True if the blob is
    confirmed absent, False if it's still present. Raises StorageError
    on transport/auth failure (separate from "still there")."""
    return _get_backend().confirm_absent(path)
```

**Backend impls:**
- `_SupabaseStorageBackend`: real Supabase Storage via `supabase-py`. Uses `SUPABASE_SERVICE_KEY` (already in env). Asserts at startup that the bucket exists, is private, and has no public-read policy.
- `_InMemoryStorageBackend`: dict-backed, for tests.

Backend selection follows the same pattern as `DocumentRepository` — env-driven (`aegis_storage_backend` already exists as `Literal["memory", "supabase"]`).

**Bucket name:** read from `AEGIS_DOCUMENT_BUCKET` env (default `documents`). Per-env separation: prod / staging / dev each get their own bucket.

**Path convention (Q5 approved):**
- With merchant: `merchants/{merchant_id}/documents/{document_id}.pdf.enc`
- Orphan (`merchant_id IS NULL`): `unassigned/documents/{document_id}.pdf.enc`
- Path stored verbatim in `documents.storage_path`. Reorganizing on merchant attach is brittle and not done — Supabase paths are content-addressable identifiers, not folder structure.

---

## 8. Worker change — chunk B

**BLOCKER 2 incorporated:** the local PDF is preserved on storage failure. `_safe_unlink` is no longer in the top-level `finally`.

```python
# src/aegis/workers.py — parse_document, after persist_parse_result(...)

try:
    repository.persist_parse_result(document_id, result=result)
    audit.record(actor="worker", action="document.parse.complete", ...)

    # Storage step. Any failure leaves the local file PRESERVED for the
    # reconcile cron to retry. Local cleanup happens only on full success.
    storage_succeeded = False
    try:
        with open(pdf_path, "rb") as f:
            plaintext = f.read()
        sha256_original = hashlib.sha256(plaintext).hexdigest()

        # CHANGE 3: catch divergence between the upload-time hash and
        # the storage-time hash. They are computed from the same source
        # of bytes; divergence indicates a code-path bug or a disk-
        # corruption event between upload and parse. Fail closed.
        doc = repository.get_document(document_id)
        if sha256_original != doc.file_hash:
            audit.record(
                actor="worker",
                action="document.original_storage_failed",
                subject_type="document",
                subject_id=document_id,
                details={
                    "reason": "sha256_divergence",
                    "sha256_original": sha256_original,
                    "file_hash": doc.file_hash,
                },
            )
            raise WorkerStorageError("sha256 divergence")

        key_version = current_key_version()
        ciphertext = encrypt_pdf(plaintext, key_version=key_version)

        storage_path = _build_storage_path(merchant_id, document_id)
        storage_objects.upload(storage_path, ciphertext)  # raises on non-2xx

        repository.persist_storage_metadata(
            document_id,
            storage_path=storage_path,
            sha256_original=sha256_original,
            encryption_key_version=key_version,
            retention_until=now_utc() + timedelta(days=365 * 7),
        )  # single atomic UPDATE — see §10 below

        audit.record(
            actor="worker",
            action="document.original_stored",
            subject_type="document",
            subject_id=document_id,
            details={
                "storage_path": storage_path,
                "encryption_key_version": key_version,
                "byte_size": len(plaintext),
                "retention_until": (now_utc() + timedelta(days=365 * 7)).isoformat(),
            },
        )
        storage_succeeded = True

    except Exception as exc:
        _log.exception(
            "worker.storage_upload_failed document_id=%s", document_id
        )
        audit.record(
            actor="worker",
            action="document.original_storage_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "error": type(exc).__name__,
                "message": str(exc)[:500],
            },
        )
        # PRESERVE the local file — move to quarantine for the
        # reconcile cron to retry.
        _quarantine_pdf(pdf_path, document_id)
        # storage_path stays NULL. Parse already succeeded; the dossier
        # just won't show a "View original PDF" link for this doc.

    if storage_succeeded:
        _safe_unlink(pdf_path)

except DocumentNotFoundError:
    # existing error path — _safe_unlink already in scope
    ...
```

**Helpers** (added to workers.py):

```python
def _build_storage_path(merchant_id: UUID | None, document_id: UUID) -> str:
    if merchant_id is None:
        return f"unassigned/documents/{document_id}.pdf.enc"
    return f"merchants/{merchant_id}/documents/{document_id}.pdf.enc"


def _quarantine_pdf(pdf_path: str, document_id: UUID) -> None:
    """Move a PDF that failed the storage step to the quarantine dir
    for the reconcile cron. Idempotent — overwrites any existing
    quarantine file for the same document_id."""
    quarantine_dir = get_settings().aegis_upload_dir / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    dest = quarantine_dir / f"{document_id}.pdf"
    Path(pdf_path).rename(dest)
    _log.info("worker.pdf_quarantined document_id=%s dest=%s", document_id, dest)
```

**Reconcile cron** (added to chunk B, runs at `hour=4, minute=0` so it's after retention sweep at `hour=3`):

```python
async def run_storage_reconcile_cron(ctx: dict[str, Any]) -> dict[str, Any]:
    """Daily 04:00 UTC. For every quarantined PDF, retry the storage
    upload. Successful retries clean up the quarantine file. Failed
    retries stay in quarantine; alert if quarantine depth > N for
    over M days (future)."""
    quarantine_dir = get_settings().aegis_upload_dir / "quarantine"
    if not quarantine_dir.exists():
        return {"retried": 0, "succeeded": 0, "still_quarantined": 0}
    # For each *.pdf in quarantine, parse the filename for document_id,
    # load the doc row, run the same encrypt+upload+persist sequence,
    # delete the quarantine file on success.
    ...
```

**Other paths that explicitly DO unlink without attempting storage** (preserved from today):
- Ambiguous processor brand → unlink (no parse output to store with)
- Unknown document_id → unlink (no doc row to anchor storage to)
- Stripe / Square processor pipeline → its own try/except/finally; storage step lands here in chunk B as well

**Test additions (chunk B):**
- `test_pdf_preserved_on_storage_upload_failure` — mock `storage_objects.upload` to raise; assert (a) local file `quarantine/{document_id}.pdf` EXISTS, (b) `documents.storage_path IS NULL`, (c) audit row `document.original_storage_failed` written, (d) parse otherwise succeeded (parse_status is terminal)
- `test_pdf_deleted_on_storage_upload_success` — mock upload + persist; assert local file gone, storage_path populated
- `test_sha256_divergence_fails_closed` — patch storage step to inject divergence; assert storage_path NULL, file quarantined, audit reason=sha256_divergence
- `test_storage_objects_upload_raises_on_non_2xx` — pure storage-helper test
- `test_reconcile_cron_retries_quarantined_pdf` — drop a quarantine file + simulate Supabase recovered; assert next reconcile run uploads + clears quarantine

---

## 9. View route — chunk C

`src/aegis/api/routes/documents.py` (new):

```python
@router.get(
    "/api/documents/{document_id}/original",
    summary="Stream the original PDF of a parsed document (operator-only).",
    description="""
    ACL v1: any authenticated commerafunding.com SSO user. Future
    expansion (per-role gating) swaps the domain check for an
    OperatorRole lookup — the audit event already carries the
    operator's email, so the upgrade is to the gate, not the trace.
    """,
)
async def view_original_pdf(
    document_id: UUID,
    request: Request,
    _tunnel: Annotated[None, Depends(require_tunnel_secret)],  # BLOCKER 1
    operator_email: Annotated[str, Depends(require_sso_user_email)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> StreamingResponse:
    # v1 ACL — domain check
    if not operator_email.endswith("@commerafunding.com"):
        audit.record(
            actor=f"operator:{operator_email}",
            action="document.original_viewed_denied",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "acl_domain",
                "ip": _client_ip(request),
                "user_agent": _ua(request),
            },
        )
        raise HTTPException(403, "access denied")

    try:
        doc = docs.get_document(document_id)
    except DocumentNotFoundError:
        audit.record(
            actor=f"operator:{operator_email}",
            action="document.original_viewed_denied",
            subject_type="document",
            subject_id=document_id,
            details={"reason": "not_found", "ip": _client_ip(request)},
        )
        raise HTTPException(404, "document not found")

    if doc.storage_path is None:
        audit.record(
            actor=f"operator:{operator_email}",
            action="document.original_viewed_denied",
            subject_type="document",
            subject_id=document_id,
            details={"reason": "no_storage_path", "ip": _client_ip(request)},
        )
        raise HTTPException(404, "original not available")

    try:
        ciphertext = storage_objects.download(doc.storage_path)
    except StorageError as exc:
        audit.record(
            actor=f"operator:{operator_email}",
            action="document.original_viewed_integrity_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "storage_download_failed",
                "error": str(exc)[:500],
                "ip": _client_ip(request),
            },
        )
        raise HTTPException(500, "storage read failed")

    try:
        plaintext = decrypt_pdf(ciphertext, key_version=doc.encryption_key_version)
    except CorruptCiphertextError as exc:
        audit.record(
            actor=f"operator:{operator_email}",
            action="document.original_viewed_integrity_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "decrypt_failed",
                "error": str(exc)[:500],
                "encryption_key_version": doc.encryption_key_version,
                "ip": _client_ip(request),
            },
        )
        raise HTTPException(500, "integrity check failed")

    if hashlib.sha256(plaintext).hexdigest() != doc.sha256_original:
        audit.record(
            actor=f"operator:{operator_email}",
            action="document.original_viewed_integrity_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "sha256_mismatch",
                "ip": _client_ip(request),
            },
        )
        raise HTTPException(500, "integrity check failed")

    audit.record(
        actor=f"operator:{operator_email}",
        action="document.original_viewed",
        subject_type="document",
        subject_id=document_id,
        details={
            "merchant_id": str(doc.merchant_id) if doc.merchant_id else None,
            "ip": _client_ip(request),
            "user_agent": _ua(request),
            "encryption_key_version": doc.encryption_key_version,
            "byte_size": len(plaintext),
        },
    )

    return StreamingResponse(
        BytesIO(plaintext),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="document-{document_id}.pdf"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
```

**New auth deps** in `src/aegis/api/auth.py`:
- `require_sso_user_email`: composes `resolve_operator_email`; 401 if None
- `require_tunnel_secret`: reads `Cf-Aegis-Tunnel-Secret` header, constant-time compare against `settings.aegis_tunnel_shared_secret`. 403 if missing or mismatched.

**Why no signed URLs:** the design explicitly does NOT use Supabase signed URLs. Operator browser → AEGIS → Supabase. Operator browser NEVER receives a Supabase URL. Reasons:
- Signed URLs are bearer tokens with a TTL — once issued, they're forwardable until expiry.
- Routing through AEGIS gives us audit-on-every-view (URL would let the browser cache and re-fetch without an audit).
- Integrity check happens in AEGIS — a signed URL would skip it.

Enforced as a **static security invariant test** (`tests/test_security_invariants.py`, new — smaller fix):

```python
def test_no_signed_url_or_public_url_methods_in_code() -> None:
    """The PDF retention design forbids exposing Supabase URLs to the
    client. Grep the codebase for create_signed_url and get_public_url
    method calls; both must produce zero matches."""
    forbidden = ("create_signed_url", "get_public_url", "createSignedUrl", "getPublicUrl")
    for term in forbidden:
        for path in Path("src/aegis").rglob("*.py"):
            assert term not in path.read_text(), (
                f"{path}: {term} is forbidden — see "
                "docs/PDF_RETENTION_DESIGN.md §9"
            )
```

---

## 10. Dossier UI — chunk D

`src/aegis/web/templates/merchant_detail_dossier.html.j2` + `merchant_detail_dossier_pdf.html.j2` (latter still applies even though the PDF dossier's drill-down shape is backlog item #2 — the link is harmless in the PDF and useful when rendered):

```jinja
{% if doc.storage_path %}
  <a href="/api/documents/{{ doc.id }}/original"
     target="_blank" rel="noopener"
     class="doc-view-original"
     data-document-id="{{ doc.id }}">
    View original PDF ↗
  </a>
{% endif %}
```

CSS in `aegis-tool.css` — subtle, function-over-affordance:

```css
.dossier-page .doc-view-original {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--ink-3);
  text-decoration: none;
  border-bottom: 1px dotted color-mix(in oklch, var(--accent) 40%, var(--rule));
  padding-bottom: 1px;
}
.dossier-page .doc-view-original:hover {
  color: var(--accent);
  border-bottom-style: solid;
}
```

`base.html.j2` cache-buster bump: `aegis-tool.css?v=14`.

**Visual verification** (chunk D acceptance):
- Pre-migration-033 documents (the ~30 legacy rows) render with NO link
- Post-migration-033 documents with successful storage render the link
- Post-migration-033 documents with failed storage render NO link (storage_path NULL)
- Click → new tab → PDF inline in browser viewer

---

## 11. Retention lifecycle — chunks B + E

### On upload (chunk B, inside the worker storage step):

```python
retention_until = now_utc() + timedelta(days=365 * 7)
# 7-year baseline. Commera internal retention policy (NOT 16 CFR §1020.220 —
# that statute binds Customer Identification Program duties on covered
# financial institutions; AEGIS as a pure ISO broker is not a covered
# institution and §1020.220 does not directly apply. The 7-year and
# 5-year numbers used here are Commera's chosen retention windows
# referenced against industry baselines, not regulatory minima.).
```

### On merchant soft-delete (chunk B, sub-step):

Triggered when `merchants.deleted_at` transitions from NULL to a timestamp. The actual UI/API to mark merchants soft-deleted is deferred; the column exists in chunk A and the extension logic exists in chunk B so that when a future feature adds the trigger, the retention math works.

```python
# In whichever code path sets merchants.deleted_at:
old_retentions = repository.list_documents_with_retention_for_merchant(merchant_id)
new_retention = now_utc() + timedelta(days=365 * 5)
repository.extend_retention_for_merchant(
    merchant_id=merchant_id,
    new_retention=new_retention,  # GREATEST applied in the SQL
)
# CHANGE 4(a): audit per document with old vs new
for doc_id, old_until in old_retentions:
    audit.record(
        actor="worker:soft_delete_extender",
        action="document.retention_extended",
        subject_type="document",
        subject_id=doc_id,
        details={
            "merchant_id": str(merchant_id),
            "triggered_by": "merchant_soft_delete",
            "old_retention_until": old_until.isoformat() if old_until else None,
            "new_retention_until": new_retention.isoformat(),
        },
    )
```

The SQL itself uses `GREATEST` (Q2 = MAX, per operator):

```python
def extend_retention_for_merchant(self, *, merchant_id: UUID, new_retention: datetime):
    self._client.table("documents").update({
        "retention_until": "GREATEST(retention_until, %s)" % new_retention.isoformat()
        # via parametrized RPC — exact mechanism depends on supabase-py
    }).eq("merchant_id", str(merchant_id)).is_("storage_path", "not.null").execute()
```

Soft-delete only EXTENDS, never shortens.

### Nightly sweep (chunk E):

```python
async def run_retention_sweep_cron(ctx: dict[str, Any]) -> dict[str, Any]:
    """Daily 03:00 UTC. Hard-deletes ciphertext from Supabase Storage
    for documents whose retention_until has passed.

    CHANGE 5 — provable deletion:
      1. Delete the blob via Supabase Storage API.
      2. Confirm absent via HEAD/list — tolerates 404 (idempotent
         retry), raises on anything else.
      3. UPDATE documents SET storage_path = NULL AND write the
         audit row in the SAME DB transaction. The audit row's
         details JSON includes deletion_confirmed: true.

    Result: a regulator records request "did you delete document X
    by date Y?" has a SQL-queryable answer with proof of confirmation.
    """
    repo = get_repository()
    audit = get_audit()
    candidates = repo.list_retention_expired(limit=1000)
    deleted = 0
    failed = 0
    for doc in candidates:
        try:
            storage_objects.delete(doc.storage_path)
            absent = storage_objects.confirm_absent(doc.storage_path)
            if not absent:
                # Storage said 2xx on delete but the blob is still
                # listable. Either eventually-consistent (treat as
                # transient — next sweep retries) or a backend bug
                # (audit and skip this row this run).
                audit.record(
                    actor="cron:retention_sweep",
                    action="document.retention_delete_failed",
                    subject_type="document",
                    subject_id=doc.id,
                    details={
                        "reason": "still_present_after_delete",
                        "storage_path": doc.storage_path,
                    },
                )
                failed += 1
                continue

            # Atomic: clear path + write audit in one tx
            with repo.transaction() as tx:
                tx.clear_storage_path(doc.id)
                tx.write_audit(
                    actor="cron:retention_sweep",
                    action="document.retention_deleted",
                    subject_type="document",
                    subject_id=doc.id,
                    details={
                        "merchant_id": str(doc.merchant_id) if doc.merchant_id else None,
                        "original_storage_path": doc.storage_path,
                        "original_retention_until": doc.retention_until.isoformat(),
                        "encryption_key_version": doc.encryption_key_version,
                        "deletion_confirmed": True,
                    },
                )
            deleted += 1

        except Exception as exc:
            audit.record(
                actor="cron:retention_sweep",
                action="document.retention_delete_failed",
                subject_type="document",
                subject_id=doc.id,
                details={
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                    "storage_path": doc.storage_path,
                },
            )
            failed += 1

    return {"deleted": deleted, "failed": failed, "candidates": len(candidates)}


# Registered in workers.py WorkerSettings.cron_jobs:
cron(run_retention_sweep_cron, hour=3, minute=0, run_at_startup=False),
```

**Idempotency:**
- `storage_objects.delete` tolerates an already-404 blob (no error)
- `storage_objects.confirm_absent` returns True on 404, False on 200
- The DB transaction means clear_storage_path + audit_log row land together or not at all (no half-state where the blob is gone but the doc still claims it has a storage_path)

**Test coverage (chunk E):**
- `test_expired_doc_gets_swept` — happy path
- `test_unexpired_doc_skipped` — guard against false positives
- `test_already_swept_doc_skipped` — `storage_path IS NULL` rows ignored
- `test_supabase_delete_failure_audits_and_keeps_storage_path` — failure mode
- `test_supabase_eventual_consistency_audits_still_present` — confirm_absent returns False
- `test_db_transaction_atomicity` — simulate clear_storage_path success + audit failure; assert rollback (or simulate vice versa)
- `test_idempotent_on_double_run` — delete then sweep again over the same doc → no error, no duplicate audit row

---

## 12. ACL + audit summary

| Event | Action | Actor | Where written |
|---|---|---|---|
| Successful view | `document.original_viewed` | `operator:{email}` | View route §9 |
| Forbidden (wrong domain) | `document.original_viewed_denied` | `operator:{email}` | View route §9 |
| Not found | `document.original_viewed_denied` (reason=not_found) | `operator:{email}` | View route §9 |
| Legacy / upload-failed | `document.original_viewed_denied` (reason=no_storage_path) | `operator:{email}` | View route §9 |
| Integrity failure | `document.original_viewed_integrity_failed` | `operator:{email}` | View route §9 |
| Successful storage at parse time | `document.original_stored` | `worker` | Worker §8 |
| Failed storage at parse time | `document.original_storage_failed` | `worker` | Worker §8 |
| sha256 divergence at parse time | `document.original_storage_failed` (reason=sha256_divergence) | `worker` | Worker §8 |
| Retention extended on soft-delete | `document.retention_extended` | `worker:soft_delete_extender` | §11 |
| Retention sweep deletion | `document.retention_deleted` (deletion_confirmed=true) | `cron:retention_sweep` | Sweep §11 |
| Retention sweep failure | `document.retention_delete_failed` | `cron:retention_sweep` | Sweep §11 |
| Reparse from storage | `document.original_reparsed_from_storage` | `operator:{email}` or `cli` | Chunk F §15 |
| Reconcile cron retry success | `document.original_stored` (reason=reconcile) | `cron:storage_reconcile` | Reconcile §8 |
| Reconcile cron retry failure | `document.original_storage_failed` (reason=reconcile) | `cron:storage_reconcile` | Reconcile §8 |

Every PDF-touching code path writes an audit row. `audit_log` is the regulator-facing answer to "what happened to document X."

---

## 13. No legacy backfill

Existing documents (~30 rows as of 2026-06-01) have:
- `storage_path = NULL`
- `sha256_original = NULL`
- `encryption_key_version = NULL`
- `retention_until = NULL`

These rows render dossiers without the "View original PDF" link (gated on `storage_path != NULL`). The operator's workflow for these docs continues to require local re-upload via `_reparse_one.py` without `--from-storage`. No backfill job; no attempt to recreate ciphertext from data we don't have.

Documented in CLAUDE.md under the new PDF posture (§16).

---

## 14. Test plan (per chunk)

### Chunk A (`tests/test_crypto.py` + `tests/test_storage_objects.py` + `tests/test_migrations_033.py` + `tests/test_security_invariants.py`)

| Test | Assertion |
|---|---|
| `test_aes_gcm_roundtrip` | `decrypt_pdf(encrypt_pdf(plaintext, v=1), v=1) == plaintext` for several sizes (1B, 1KB, 25MB) |
| `test_same_plaintext_different_ciphertext` | Two `encrypt_pdf(b"same", v=1)` calls produce distinct blobs (nonce randomness) |
| `test_key_mismatch_raises` | `decrypt_pdf(ct_v1, v=2)` raises `CorruptCiphertextError` |
| `test_tampered_ciphertext_raises` | Flip one byte in a sealed blob → `CorruptCiphertextError` |
| `test_blob_shorter_than_28_bytes_raises` | `decrypt_pdf(b"x" * 10, v=1)` raises immediately |
| `test_key_must_decode_to_32_bytes` | Boot guard: `PDF_ENCRYPTION_KEY_V1` decoding to 31 or 33 bytes → `CryptoConfigError` |
| `test_current_key_version_must_be_defined` | `PDF_ENCRYPTION_KEYS_CURRENT=99` with no V99 → `CryptoConfigError` at boot |
| `test_storage_objects_upload_download_roundtrip` | InMemory backend |
| `test_storage_objects_upload_raises_on_non_2xx` | Mock Supabase 500 → `StorageError` |
| `test_storage_objects_delete_idempotent_on_404` | Mock Supabase 404 → no error |
| `test_storage_objects_confirm_absent_returns_true_on_404` | Mock Supabase 404 → True |
| `test_bucket_private_assertion_at_startup` | Backend reports public bucket → `RuntimeError` at lifespan start |
| `test_migration_033_columns_present` | psql introspection |
| `test_migration_033_partial_index_predicate` | `pg_indexes` lookup confirms `WHERE storage_path IS NOT NULL` |
| `test_no_signed_url_or_public_url_methods_in_code` | Source grep (smaller fix) |
| `test_no_retained_forever_anomaly` | `SELECT COUNT(*) FROM documents WHERE storage_path IS NOT NULL AND retention_until IS NULL = 0` — runs against in-mem fixture |

### Chunk B — see §8.

### Chunk C — see §9 + the test list in §15 of the original surface.

### Chunk D — visual + 1 unit test that asserts presence/absence of the link.

### Chunk E — see §11.

### Chunk F — see §15.

---

## 15. Operator scripts — chunk F (may slip to a separate session)

Both `scripts/_reparse_one.py` and `scripts/_reparse_wipe.py` gain `--from-storage`:

```python
parser.add_argument(
    "--from-storage",
    action="store_true",
    help=(
        "Fetch encrypted PDF from Supabase Storage and decrypt locally"
        " before re-parse. Requires the document's storage_path to be"
        " populated (migration 033 / post-2026-06-01 docs). Operator"
        " local copy not required."
    ),
)
```

Behavior when `--from-storage` is set:

```python
if args.from_storage:
    doc = sb.table("documents").select(
        "id, storage_path, sha256_original, encryption_key_version, original_filename"
    ).eq("id", str(doc_uuid)).execute().data[0]

    if doc["storage_path"] is None:
        sys.exit("ERROR: --from-storage requires storage_path; this doc has none "
                 "(legacy or upload-failed). Use without --from-storage and "
                 "supply the local PDF.")

    ciphertext = storage_objects.download(doc["storage_path"])
    plaintext = decrypt_pdf(ciphertext, key_version=doc["encryption_key_version"])

    actual_sha = hashlib.sha256(plaintext).hexdigest()
    if actual_sha != doc["sha256_original"]:
        sys.exit("ERROR: sha256 mismatch on decrypt — integrity check failed. "
                 "Aborting without re-parse.")

    # CHANGE 4(b): audit every from-storage decrypt
    audit.record(
        actor=f"cli:{os.environ.get('USER', 'unknown')}",
        action="document.original_reparsed_from_storage",
        subject_type="document",
        subject_id=doc_uuid,
        details={
            "encryption_key_version": doc["encryption_key_version"],
            "storage_path": doc["storage_path"],
            "byte_size": len(plaintext),
            "trigger": "_reparse_one --from-storage"
                if "reparse_one" in sys.argv[0]
                else "_reparse_wipe --from-storage",
        },
    )

    # Write to temp + enqueue / call parse_document directly
    ...
else:
    # existing behavior: confirm YES, wipe rows, prompt operator to re-upload
    ...
```

Tests:
- `test_reparse_from_storage_happy_path` — full decrypt + re-parse cycle
- `test_reparse_from_storage_no_storage_path_errors_cleanly` — clear message, no wipe
- `test_reparse_from_storage_sha256_mismatch_aborts` — corruption guard
- `test_reparse_from_storage_writes_audit_row` — `document.original_reparsed_from_storage` present

---

## 16. CLAUDE.md update — lands with chunk A

Replace the day-one rule:

> - **Never store PDFs long-term.** Parse → extract → delete from disk in a `finally` block. DB stores transactions and metadata, not the PDF.

with:

> - **PDF storage posture.** Local disk: parse → extract → delete (preserved on storage-step failure for the reconcile cron to retry; unconditional finally-delete is OBSOLETE as of migration 033). Long-term: encrypted ciphertext in Supabase Storage via AEGIS-managed client-side AES-256-GCM with versioned keys (`/etc/aegis/aegis.env PDF_ENCRYPTION_KEY_V{n}`). Compromised Supabase = ciphertext only; compromised box = full disclosure (honest threat-model boundary — keys and storage creds share aegis.env; mitigating this needs KMS, deferred). View access goes through `GET /api/documents/{id}/original`, SSO-authenticated + ACL-domain-gated, with shared-secret tunnel header `Cf-Aegis-Tunnel-Secret` enforced — NEVER via Supabase signed URLs (`test_security_invariants.py` enforces). SHA-256 of plaintext anchored at `documents.sha256_original`, checked on every read; mismatch = 500 + `document.original_viewed_integrity_failed`. Retention enforced by nightly arq cron `run_retention_sweep_cron`: hard-delete ciphertext from Supabase + HEAD-confirm absent + atomic DB clear-and-audit (`deletion_confirmed: true` in audit details). Baseline 7 years from upload; extended to ≥ 5 years from merchant soft-delete via `GREATEST` (Commera internal policy, not a CIP-rule binding — AEGIS is not a covered financial institution under 16 CFR §1020.220). Every PDF-touching code path writes an audit row — see `docs/PDF_RETENTION_DESIGN.md §12`. Legacy docs (pre-033) carry `storage_path IS NULL` and degrade to no-original-available; `_reparse_*.py --from-storage` only works on post-033 docs.

---

## 17. Key rotation + backup posture

`docs/PDF_KEY_ROTATION.md` (new, lands with chunk A):

- Generate new key (`openssl rand -base64 32`)
- Add `PDF_ENCRYPTION_KEY_V2=...` to `/etc/aegis/aegis.env`
- Set `PDF_ENCRYPTION_KEYS_CURRENT=2`
- `systemctl restart aegis-web aegis-worker`
- All new uploads use V2; existing blobs decrypt via their stored `encryption_key_version`
- Optional lazy re-encryption: on view, if `encryption_key_version < current`, re-encrypt + UPDATE
- Optional batch re-encryption: off-hours job (not built)
- Old key retirement: only after every documents row references a newer version

Backup posture: Supabase Storage durability (11 nines per SLA) is accepted for v1. A future secondary backend (S3, Backblaze B2) requires no re-encryption — ciphertext is identical regardless of where it's stored.

---

## 18. Smaller fixes folded in

| Operator note | Resolved |
|---|---|
| Show `os.urandom(12)` per-encryption | §6 code block — explicit `nonce = os.urandom(_NONCE_BYTES)` and `test_same_plaintext_different_ciphertext` |
| `decrypt_pdf` rejects blobs < 28 bytes | §6 code: `if len(blob) < _MIN_BLOB_BYTES: raise` + `test_blob_shorter_than_28_bytes_raises` |
| Boot validates each key decodes to exactly 32 bytes | §6 `_decode_key` length check + `test_key_must_decode_to_32_bytes` |
| "No signed URLs" as static invariant test | §9 `tests/test_security_invariants.py::test_no_signed_url_or_public_url_methods_in_code` |
| db_verify check: storage_path NOT NULL AND retention_until NULL | §5 probe `migration-033-no-retained-forever-anomaly.sql` |
| Bucket PRIVATE service_role-only; assert at startup | §7 `assert_bucket_private_at_startup` called by app.lifespan + `test_bucket_private_assertion_at_startup` |
| Confirm `persist_storage_metadata` is single atomic UPDATE | §8 + repository impl will be one `.update().eq(id)` call writing all four columns together. Test: `test_persist_storage_metadata_atomic` snapshots that storage_path and retention_until are set in the same DB transaction (no half-state where one is set without the other). |
| Reframe retention as Commera internal policy NOT 16 CFR §1020.220 | §11 + §16 reworded — CIP binds covered financial institutions; AEGIS as a pure ISO broker is NOT a covered institution. The 7y/5y numbers stay (Commera's chosen retention), `GREATEST` extends-never-shortens stays. |

---

## 19. Decisions captured (from operator's Q answers)

| Q | Decision |
|---|---|
| Q1: soft-delete column | **Option A** — add `deleted_at TIMESTAMPTZ NULL` to merchants in migration 033 |
| Q2: retention semantics on soft-delete | **MAX / GREATEST** — extends never shortens |
| Q3: key management | **env-var keys for v1** with honest threat-model wording: keys + storage creds share `aegis.env`, so "ciphertext only" holds for a Supabase breach but not a box breach |
| Q4: bucket scope | **single bucket per env** via `AEGIS_DOCUMENT_BUCKET` (default `documents`); PRIVATE; service_role-only |
| Q5: orphan path | `unassigned/documents/{document_id}.pdf.enc` |

---

## 20. Chunk-by-chunk plan

| Chunk | Files (new/modified) | Acceptance |
|---|---|---|
| **A — Plumbing (no user-visible change)** | `migrations/033_documents_storage_and_retention.sql`, `scripts/db_checks/migration-033-*.sql` (3 probes registered in `MIGRATION_PROBES`), `src/aegis/crypto.py` (new), `src/aegis/storage_objects.py` (new), `src/aegis/config.py` (new env vars + boot validators), `pyproject.toml` (+ cryptography>=44), `CLAUDE.md` (PDF posture rewrite), `docs/PDF_KEY_ROTATION.md` (new), `tests/test_crypto.py`, `tests/test_storage_objects.py`, `tests/test_migrations_033.py`, `tests/test_security_invariants.py` (all new). | `make check` PASS; `db_verify --check migration-033-*` PASS on prod after deploy; ufw status verified to default-deny + 5555 not in allow list (runtime check, added to runbook); `assert_bucket_private_at_startup` runs at boot without crashing. NO behavioral change to existing routes. |
| **B — Worker writes** | `src/aegis/workers.py` (storage step + quarantine helper + reconcile cron), `src/aegis/storage.py` (`persist_storage_metadata`, `list_retention_expired`, `clear_storage_path`, `extend_retention_for_merchant`, `list_documents_with_retention_for_merchant`, transaction context), `tests/test_workers.py` (additions + new failure-mode tests). | New parses produce `storage_path` populated; upload failures preserve local file under `quarantine/` and audit `original_storage_failed`; sha256-divergence fails closed; reconcile cron retries quarantined uploads on next run; `make check` PASS. |
| **C — View route** (gated on BLOCKER 1 lockdown verified at deploy time) | `src/aegis/api/routes/documents.py` (new), `src/aegis/api/auth.py` (`require_sso_user_email`, `require_tunnel_secret`), `src/aegis/api/app.py` (router register), `tests/test_api_documents_view.py` (new), `deploy/cloudflared-config.yml.example` (sample config injecting the shared-secret header). | Operator browser → `/api/documents/{id}/original` → 200 with PDF inline; all forbidden / not-found / integrity-failed paths audit; tunnel-secret missing → 403; SSO header missing → 401. `make check` PASS. Pre-deploy: ufw status confirmed + tunnel secret confirmed populated on box + cloudflared config confirmed injecting the header. |
| **D — Dossier UI** | `src/aegis/web/templates/merchant_detail_dossier.html.j2`, `src/aegis/web/templates/merchant_detail_dossier_pdf.html.j2`, `src/aegis/web/static/aegis-tool.css`, `src/aegis/web/templates/base.html.j2` (cache-buster). 1 unit test for link presence/absence. | Visual verification on a real merchant dossier: link appears for storage_path-set docs, hidden for legacy/failed; click opens new tab to inline PDF. |
| **E — Retention sweep cron** (may slip to a separate session) | `src/aegis/retention_sweep.py` (new), `src/aegis/workers.py` (cron registration at `hour=3, minute=0`), `tests/test_retention_sweep.py` (new — full coverage of expired/not/already-swept/Supabase-failure/EC/transactional atomicity/idempotency). | Cron registered; tests pass; smoke against a manually-aged fixture row confirms full delete + audit cycle. |
| **F — Operator scripts** (may slip) | `scripts/_reparse_one.py`, `scripts/_reparse_wipe.py`, `tests/test_reparse_from_storage.py` (new). | `--from-storage` decrypts + writes audit row + enqueues parse without operator-local PDF; clear error when `storage_path IS NULL`. |

---

## 21. Out of scope for this build (documented; not built)

- AWS KMS or HSM integration (keys move off-disk)
- Lazy re-encryption on view
- Batch re-encryption job
- Secondary backup backend (S3, Backblaze B2 mirror)
- Public per-role ACL (operator/role taxonomy)
- Public UI for marking merchants soft-deleted (column exists; trigger TBD)
- Backfill of legacy ~30 documents (no PDF to recover)

---

**Awaiting chunk A authorization.**
