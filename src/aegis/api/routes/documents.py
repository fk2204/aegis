"""GET /api/documents/{document_id}/original — encrypted-PDF view route.

Chunk C of the PDF retention redesign. Operator opens a "View original
PDF" link in the dashboard → browser hits this route under Cloudflare
Access SSO → AEGIS verifies ACL, downloads ciphertext from Supabase
Storage, decrypts in-process, runs a SHA-256 integrity check against
``documents.sha256_original``, audits, and streams plaintext bytes back.

Server-side decrypt only. Plaintext NEVER leaves the AEGIS process
except as the streamed response body. NO Supabase signed URLs, NO
public URLs — locked down at CI by
``tests/test_security_invariants.py::test_no_supabase_signed_or_public_url_helpers_in_source``.

Auth layers in order of evaluation:

1. ``require_sso_user_email`` — Cloudflare Access SSO identity must be
   present (the ``cf-access-authenticated-user-email`` header). 401
   otherwise.
2. Domain ACL — email must end with ``@commerafunding.com``. 403 +
   ``document.original_viewed_denied`` (reason ``acl_domain``)
   otherwise.

ACL v1 is a domain check. Future hardening swaps the domain test for an
``operators`` table lookup (per-role gate via ``OperatorRole``). The
audit row already carries ``actor_email`` so the trace is identical
under both v1 and v2 — only the gate logic changes.

Future hardening — tunnel-secret header (NOT enforced in this chunk):
once cloudflared injects ``Cf-Aegis-Tunnel-Secret`` at the tunnel edge,
a third dependency will require constant-time match against
``AEGIS_TUNNEL_SHARED_SECRET`` before the ACL runs. Deferring it now
because the load-bearing control (origin loopback bind, verified by
``assert_uvicorn_loopback_bind``) is already in place, and adding the
tunnel-secret requires a coordinated cloudflared deploy. See
``docs/PDF_RETENTION_DESIGN.md`` §9.
"""

from __future__ import annotations

import hashlib
import re
from io import BytesIO
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from aegis.api.auth import require_sso_user_email
from aegis.api.deps import get_audit, get_repository
from aegis.audit import AuditLog
from aegis.crypto import CorruptCiphertextError, decrypt_pdf
from aegis.logger import get_logger
from aegis.storage import DocumentNotFoundError, DocumentRepository, DocumentRow
from aegis.storage_objects import StorageError, download

_log = get_logger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

_ALLOWED_DOMAIN = "@commerafunding.com"
_USER_AGENT_CAP = 200

# Forbid CR/LF/quote/backslash in the filename slot of Content-Disposition
# so a maliciously-named upload can't inject a second header. Keep the
# allowlist permissive (ASCII printable minus those four) — non-ASCII
# filenames fall back to the safe default below.
_FILENAME_FORBID_RE = re.compile(r'[\x00-\x1f"\\\r\n]')
_FILENAME_FALLBACK = "document.pdf"


@router.get(
    "/{document_id}/original",
    summary="Stream the decrypted original PDF for a document.",
    response_class=StreamingResponse,
)
async def get_original_pdf(
    document_id: UUID,
    request: Request,
    operator_email: Annotated[str, Depends(require_sso_user_email)],
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> StreamingResponse:
    """Decrypt + stream the stored PDF for one document.

    Response shape on success: ``application/pdf`` body with
    ``Content-Disposition: inline; filename=...``,
    ``Cache-Control: private, no-store`` (browser must not cache the
    plaintext), ``X-Content-Type-Options: nosniff`` (defense-in-depth
    against mis-sniffed content type).

    Status code map:

    * 200 — happy path, stream the plaintext.
    * 401 — no SSO header (handled by ``require_sso_user_email``).
    * 403 — SSO present but ACL fails. Audit
      ``document.original_viewed_denied`` (reason ``acl_domain``).
    * 404 — document not found, OR document exists but
      ``storage_path IS NULL`` (legacy / pre-chunk-B / parse failure
      that quarantined). The storage_path-null case audits
      ``document.original_viewed_denied`` (reason ``no_storage_path``)
      so the operator can see why the link 404s; the missing-doc case
      does not (no subject row to attach the trace to).
    * 500 — integrity failure. Three sub-cases, all audited as
      ``document.original_viewed_integrity_failed``:

        - ``decrypt_invalid_tag``: AES-GCM rejects the tag. Wrong key,
          ciphertext tampered, or blob bit-flipped in storage.
        - ``sha256_mismatch``: GCM tag verified, but the plaintext's
          SHA-256 disagrees with ``documents.sha256_original``. This
          means the encrypt-time hash and the decrypt-time hash
          disagree — usually a re-encrypt-time bug, since GCM already
          authenticated the ciphertext.
        - ``storage_download_failed``: Supabase Storage download
          raised. Treated as integrity-failed (the ciphertext we
          expected isn't there).

    The integrity failure action is intentionally a SEPARATE audit
    action from the success ``document.original_viewed`` so an alert
    rule can fire on the failure action without filtering successes.

    Plaintext lifecycle: read into memory once (``download`` →
    ``decrypt_pdf`` → ``BytesIO``), streamed back, then garbage
    collected when the response closes. No plaintext is logged, no
    plaintext is written to disk, no plaintext leaves the process.
    """
    actor = f"operator:{operator_email}"

    # Layer 2: domain ACL. Future swap to OperatorRole lookup keeps the
    # audit trace identical (actor_email already carries the gate input).
    if not operator_email.endswith(_ALLOWED_DOMAIN):
        audit.record(
            actor=actor,
            actor_email=operator_email,
            action="document.original_viewed_denied",
            subject_type="document",
            subject_id=document_id,
            details={"reason": "acl_domain"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # Layer 3: document existence + storage_path populated. Missing doc
    # → silent 404 (no subject to audit). storage_path NULL → 404 with
    # an explicit denied audit so the operator can see why the link
    # didn't resolve.
    try:
        doc = repository.get_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        ) from exc

    if doc.storage_path is None or doc.encryption_key_version is None:
        audit.record(
            actor=actor,
            actor_email=operator_email,
            action="document.original_viewed_denied",
            subject_type="document",
            subject_id=document_id,
            details={"reason": "no_storage_path"},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document has no stored original",
        )

    # Layer 4: storage download. Failure here is rare (the storage_path
    # was populated atomically with the upload success) but if the blob
    # is gone we treat it as integrity-failed — the row says ciphertext
    # exists; it doesn't; that disagreement is the integrity violation.
    try:
        ciphertext = download(doc.storage_path)
    except StorageError as exc:
        audit.record(
            actor=actor,
            actor_email=operator_email,
            action="document.original_viewed_integrity_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "storage_download_failed",
                "storage_path": doc.storage_path,
                "error_type": type(exc).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Storage download failed",
        ) from exc

    # Layer 5: AES-GCM decrypt. CorruptCiphertextError wraps the
    # cryptography library's InvalidTag (and short-blob defensive guard).
    try:
        plaintext = decrypt_pdf(
            ciphertext, key_version=doc.encryption_key_version
        )
    except CorruptCiphertextError as exc:
        audit.record(
            actor=actor,
            actor_email=operator_email,
            action="document.original_viewed_integrity_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "decrypt_invalid_tag",
                "encryption_key_version": doc.encryption_key_version,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Integrity check failed",
        ) from exc

    # Layer 6: second integrity check. AES-GCM's tag already
    # authenticates the ciphertext, but the SHA-256 stored at upload
    # time is the independent hash of the plaintext bytes. A mismatch
    # here means either (a) the worker stored a hash that doesn't
    # match what it encrypted (encryption-time bug), or (b) someone
    # decrypted+re-encrypted with the right key but different
    # plaintext. Both are catastrophic — refuse the response.
    if doc.sha256_original is None or (
        hashlib.sha256(plaintext).hexdigest() != doc.sha256_original
    ):
        audit.record(
            actor=actor,
            actor_email=operator_email,
            action="document.original_viewed_integrity_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "sha256_mismatch",
                "encryption_key_version": doc.encryption_key_version,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Integrity check failed",
        )

    # Success — audit the view BEFORE returning the response. If the
    # audit insert fails (SupabaseAuditLog raises AuditWriteError) the
    # operator gets a 500 and no PDF is streamed; this matches
    # CLAUDE.md's "audit-write failures fail the operation" rule.
    client_host = request.client.host if request.client is not None else None
    user_agent = (request.headers.get("user-agent") or "")[:_USER_AGENT_CAP]
    audit.record(
        actor=actor,
        actor_email=operator_email,
        action="document.original_viewed",
        subject_type="document",
        subject_id=document_id,
        details={
            "merchant_id": str(doc.merchant_id) if doc.merchant_id else None,
            "ip": client_host,
            "user_agent": user_agent,
            "encryption_key_version": doc.encryption_key_version,
        },
    )

    return StreamingResponse(
        BytesIO(plaintext),
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition(doc),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _content_disposition(doc: DocumentRow) -> str:
    """Build a safe ``inline; filename="..."`` header value.

    The original filename comes from operator-uploaded multipart form
    data and is stored verbatim on the row. Header injection
    (CR/LF), broken-quote escaping (``"``, ``\\``), and control
    characters are scrubbed; if nothing survives the filter we fall
    back to a constant. Browsers that don't understand the safe
    filename get the fallback — a deliberate trade against a
    user-visible name to keep the response header well-formed.
    """
    raw = (doc.original_filename or "").strip()
    cleaned = _FILENAME_FORBID_RE.sub("", raw)
    safe = cleaned if cleaned else _FILENAME_FALLBACK
    return f'inline; filename="{safe}"'


__all__ = ["router"]
