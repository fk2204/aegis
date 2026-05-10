"""POST /upload — accept a PDF, dedupe by hash, enqueue parse.

Flow
----
1. Read up to ``aegis_max_upload_bytes`` from the request body. Anything
   larger is rejected before it lands on disk.
2. Hash the bytes with sha256. If a document with that hash already
   exists, return its row with HTTP 200 (idempotent re-upload).
3. Otherwise: write the bytes to ``aegis_upload_dir`` under a fresh UUID
   filename — the original client-supplied filename is recorded in the
   DB row but NEVER used in any path operation (per CLAUDE.md security
   rule). Insert the document row.
4. Audit ``document.upload`` and enqueue the parse job. Return HTTP 202.

Per CLAUDE.md the temp PDF must be deleted in a finally block. The worker
owns deletion when the parse runs; this route only deletes if the enqueue
fails after the file write.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit, get_repository
from aegis.audit import AuditLog
from aegis.config import get_settings
from aegis.logger import get_logger
from aegis.storage import DocumentExistsError, DocumentRepository

router = APIRouter(prefix="/upload", tags=["upload"], dependencies=[Depends(require_bearer)])

_log = get_logger(__name__)

_PDF_MAGIC = b"%PDF-"


class UploadResponse(BaseModel):
    """Response body for POST /upload."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    parse_status: str
    duplicate_of_existing: bool


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UploadResponse,
    summary="Upload a bank statement PDF and enqueue parsing.",
)
async def upload_pdf(
    request: Request,
    file: Annotated[UploadFile, File(description="Bank statement PDF")],
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchant_id: Annotated[UUID | None, Form()] = None,
) -> UploadResponse:
    """Single-file bearer-protected upload.

    The dashboard's no-bearer multi-file path (``POST /ui/upload``) calls
    ``persist_pdf_upload`` directly and bypasses this route, but exposes
    the same hashing/dedup/audit semantics via the shared helper.
    """
    settings = get_settings()
    actor = _resolve_actor(request)
    body = await _read_with_cap(file, settings.aegis_max_upload_bytes)
    return await persist_pdf_upload(
        request=request,
        body=body,
        original_filename=file.filename or "unknown.pdf",
        repository=repository,
        audit=audit,
        actor=actor,
        merchant_id=merchant_id,
    )


async def persist_pdf_upload(
    *,
    request: Request,
    body: bytes,
    original_filename: str,
    repository: DocumentRepository,
    audit: AuditLog,
    actor: str,
    merchant_id: UUID | None = None,
) -> UploadResponse:
    """Hash, dedup, persist + audit + enqueue a single PDF.

    Shared by the bearer ``POST /upload`` and the dashboard
    ``POST /ui/upload`` and ``POST /ui/intake`` routes. Raises
    ``HTTPException`` on size/format violations so callers don't have to
    re-implement the validation. The caller owns reading bytes off the
    wire (so multi-file batches can enforce a per-batch total cap).
    """
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="upload is empty"
        )
    if not body.startswith(_PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="upload is not a PDF (missing %PDF- magic bytes)",
        )

    settings = get_settings()
    file_hash = hashlib.sha256(body).hexdigest()

    existing = repository.find_by_hash(file_hash)
    if existing is not None:
        audit.record(
            actor=actor,
            action="document.upload.duplicate",
            subject_type="document",
            subject_id=existing.id,
            details={"file_hash": file_hash, "byte_size": len(body)},
        )
        return UploadResponse(
            document_id=existing.id,
            parse_status=existing.parse_status,
            duplicate_of_existing=True,
        )

    upload_dir = settings.aegis_upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / f"{uuid4().hex}.pdf"

    try:
        temp_path.write_bytes(body)
        try:
            row = repository.create_document(
                file_hash=file_hash,
                byte_size=len(body),
                original_filename=original_filename,
                uploaded_by=actor,
                merchant_id=merchant_id,
            )
        except DocumentExistsError:
            existing = repository.find_by_hash(file_hash)
            if existing is None:
                raise
            return UploadResponse(
                document_id=existing.id,
                parse_status=existing.parse_status,
                duplicate_of_existing=True,
            )

        audit.record(
            actor=actor,
            action="document.upload",
            subject_type="document",
            subject_id=row.id,
            details={
                "file_hash": file_hash,
                "byte_size": len(body),
                "original_filename": original_filename,
                "merchant_id": str(merchant_id) if merchant_id else None,
            },
        )

        await _enqueue_parse_job(request, document_id=row.id, pdf_path=str(temp_path))

        return UploadResponse(
            document_id=row.id,
            parse_status=row.parse_status,
            duplicate_of_existing=False,
        )
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                _log.warning("upload.cleanup_failed path=%s", temp_path)
        raise


# Helpers ---------------------------------------------------------------------


async def _read_with_cap(upload: UploadFile, cap_bytes: int) -> bytes:
    """Read up to cap+1 bytes; raise 413 if the cap is exceeded."""
    body = await upload.read(cap_bytes + 1)
    if len(body) > cap_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"upload exceeds {cap_bytes} bytes",
        )
    if len(body) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upload is empty",
        )
    return body


def _resolve_actor(request: Request) -> str:
    """Identify who initiated this upload for the audit log.

    Until SSO claims are wired in, we record a stable, non-reversible
    fingerprint of the bearer token (first 8 hex chars of SHA-256) so
    audit rows still distinguish operators using different tokens
    without ever logging token material. Authorization header is
    already validated by the router-level ``require_bearer`` dependency.
    """
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not token:
        return "system"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"token:{digest[:8]}"


async def _enqueue_parse_job(
    request: Request, *, document_id: UUID, pdf_path: str
) -> None:
    """Enqueue ``parse_document`` on the arq queue.

    Uses ``request.app.state.arq_pool`` if present (set in app startup) so
    the queue is mockable in tests. If the pool is missing we degrade to
    in-process enqueue: write a callable to ``request.app.state.pending_jobs``
    so tests can drain it deterministically.
    """
    pool: Any | None = getattr(request.app.state, "arq_pool", None)
    if pool is not None:
        await pool.enqueue_job("parse_document", str(document_id), pdf_path)
        return

    pending = getattr(request.app.state, "pending_jobs", None)
    if pending is None:
        pending = []
        request.app.state.pending_jobs = pending
    pending.append({"document_id": str(document_id), "pdf_path": pdf_path})


__all__ = ["UploadResponse", "persist_pdf_upload", "router"]
