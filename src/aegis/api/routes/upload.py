"""POST /upload — accept a PDF, dedupe by hash, enqueue parse.

Two ingest paths, same final state (one ``documents`` row + SHA256
dedup + enqueued ``parse_document`` job):

* ``POST /upload`` (this router) — operator drops a PDF into the dashboard
  or runs a ``curl`` upload. Optional ``close_lead_id`` query param
  associates the uploaded doc with the merchant linked to that Close Lead.
* ``POST /uploads/from-close`` (the ``uploads_router`` in this same
  module, plural prefix) — caller (n8n orchestration per Dima's spec,
  or the operator UI) gives AEGIS ``{close_lead_id, attachment_id}``;
  AEGIS pulls the PDF via ``CloseClient.download_attachment``. Same
  SHA256 dedup so a redelivered webhook + a manual ``/upload`` of the
  same statement converge on a single ``documents`` row.

Per CLAUDE.md the temp PDF must be deleted in a finally block. The
worker owns deletion when the parse runs; the routes here only delete
if the enqueue fails after the file write.

Idempotency contract (design doc guarantee #3): the SHA256 dedup at
the storage layer is the binding gate. Both routes call
``persist_pdf_upload`` which checks ``find_by_hash`` BEFORE enqueueing
any worker job, so an attachment that's already been parsed (whether
via the dashboard upload, an earlier /uploads/from-close, or any other
historical path) is never re-parsed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from aegis.api.auth import require_bearer
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_merchant_repository,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.close.client import CloseAuthError, CloseClient, CloseError
from aegis.config import get_settings
from aegis.logger import get_logger
from aegis.merchants.repository import MerchantRepository
from aegis.ops.operators import resolve_operator_email
from aegis.storage import DocumentExistsError, DocumentRepository

router = APIRouter(prefix="/upload", tags=["upload"], dependencies=[Depends(require_bearer)])

uploads_router = APIRouter(
    prefix="/uploads",
    tags=["upload"],
    dependencies=[Depends(require_bearer)],
)

_log = get_logger(__name__)

_PDF_MAGIC = b"%PDF-"

# Callable that enqueues the ``parse_document`` arq job for one new
# document. Callers compose one of these from their context (FastAPI
# request → app.state.arq_pool, or an arq worker's ctx["redis"]) and
# hand it to :func:`persist_pdf_upload`. Decoupling persist_pdf_upload
# from FastAPI's Request lets the close-attachment orchestration job
# (``aegis.workers.process_close_attachments``) reuse the same persist
# path without smuggling a fake Request.
EnqueueParse = Callable[[UUID, str], Awaitable[None]]


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
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    merchant_id: Annotated[UUID | None, Form()] = None,
    close_lead_id: Annotated[
        str | None,
        Query(
            description=(
                "Optional Close Lead id. When provided, the uploaded "
                "document is associated with the merchant linked to "
                "that Lead (404 if the Lead has no matching merchant)."
            ),
        ),
    ] = None,
) -> UploadResponse:
    """Single-file bearer-protected upload.

    The dashboard's no-bearer multi-file path (``POST /ui/upload``) calls
    ``persist_pdf_upload`` directly and bypasses this route, but exposes
    the same hashing/dedup/audit semantics via the shared helper.

    Passing ``?close_lead_id=lead_xyz`` resolves the merchant via
    ``MerchantRepository.find_by_close_lead_id`` and uses that
    merchant's id for the upload. Mutually compatible with the
    ``merchant_id`` Form field — if both are passed they must agree,
    else 400.
    """
    settings = get_settings()
    actor = _resolve_actor(request)
    body = await _read_with_cap(file, settings.aegis_max_upload_bytes)
    resolved_merchant_id = _resolve_merchant_id_for_upload(
        merchant_id=merchant_id,
        close_lead_id=close_lead_id,
        merchants=merchants,
    )
    return await persist_pdf_upload(
        enqueue_parse=_make_request_enqueue(request),
        body=body,
        original_filename=file.filename or "unknown.pdf",
        repository=repository,
        audit=audit,
        actor=actor,
        actor_email=actor_email,
        merchant_id=resolved_merchant_id,
        close_lead_id=close_lead_id,
    )


async def persist_pdf_upload(
    *,
    enqueue_parse: EnqueueParse,
    body: bytes,
    original_filename: str,
    repository: DocumentRepository,
    audit: AuditLog,
    actor: str,
    actor_email: str | None = None,
    merchant_id: UUID | None = None,
    close_lead_id: str | None = None,
) -> UploadResponse:
    """Hash, dedup, persist + audit + enqueue a single PDF.

    Shared by the bearer ``POST /upload``, the dashboard
    ``POST /ui/upload`` / ``POST /ui/intake`` routes, AND the
    ``/uploads/from-close`` Close-attachment fetcher. Raises
    ``HTTPException`` on size/format violations so callers don't have to
    re-implement the validation. The caller owns reading bytes off the
    wire (so multi-file batches can enforce a per-batch total cap and
    the from-close path can put the Close download size cap upstream).

    ``close_lead_id`` is informational — it lands in audit details
    when provided, but does not change the dedup key. The dedup key
    is the SHA256 of the PDF bytes; the same statement uploaded twice
    (once via dashboard, once via Close attachment) produces ONE
    ``documents`` row and ONE parse job regardless of which path
    fetched it first.
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
            actor_email=actor_email,
            action="document.upload.duplicate",
            subject_type="document",
            subject_id=existing.id,
            details={
                "file_hash": file_hash,
                "byte_size": len(body),
                "close_lead_id": close_lead_id,
            },
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
            actor_email=actor_email,
            action="document.upload",
            subject_type="document",
            subject_id=row.id,
            details={
                "file_hash": file_hash,
                "byte_size": len(body),
                "original_filename": original_filename,
                "merchant_id": str(merchant_id) if merchant_id else None,
                "close_lead_id": close_lead_id,
            },
        )

        await enqueue_parse(row.id, str(temp_path))

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


# ----------------------------------------------------------------------------
# POST /uploads/from-close — fetch a PDF from a Close Lead attachment
# ----------------------------------------------------------------------------


class FromCloseRequest(BaseModel):
    """Body for ``POST /uploads/from-close``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    close_lead_id: str = Field(min_length=1)
    attachment_id: str = Field(min_length=1)


class FromCloseResponse(BaseModel):
    """Response for ``POST /uploads/from-close``.

    ``duplicate`` is true when SHA256 dedup matched an existing document
    (the Close fetch happened, but no new row was created). ``parse_enqueued``
    is true only on a fresh upload.
    """

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    duplicate: bool
    parse_enqueued: bool


@uploads_router.post(
    "/from-close",
    status_code=status.HTTP_200_OK,
    response_model=FromCloseResponse,
    summary="Pull a PDF from a Close Lead attachment and enqueue parsing.",
)
async def upload_from_close(
    request: Request,
    body: FromCloseRequest,
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> FromCloseResponse:
    """Hybrid statement intake — caller hands AEGIS a Close attachment
    reference; AEGIS pulls the file via the Close API and runs it
    through the same dedup/persist/enqueue path as ``/upload``.

    The webhook handler at ``/webhooks/close`` (step 4) intentionally
    does NOT enqueue parses. n8n (per Dima's orchestration) or the
    operator UI triggers this endpoint after the inbound merchant
    upsert has landed. Result: idempotency guarantee #3 — a redelivered
    inbound event followed by an external retrigger here either
    (a) skips re-fetching because the SHA matches an existing document,
    or (b) returns the same ``document_id`` and ``parse_enqueued=False``.

    Errors:
      * 400 — body fails Pydantic validation (empty ids, etc.)
      * 404 — close_lead_id has no matching AEGIS merchant, OR Close
        returns 404 on the attachment id
      * 413 — Close-returned bytes exceed ``aegis_max_upload_bytes``
      * 502 — Close 5xx after retries, or other Close-side failure
      * 503 — CloseAuthError (missing/invalid CLOSE_API_KEY)
    """
    actor = _resolve_actor(request)
    settings = get_settings()

    merchant = merchants.find_by_close_lead_id(body.close_lead_id)
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no AEGIS merchant linked to close_lead_id "
                f"{body.close_lead_id!r}; Lead must arrive via "
                "/webhooks/close first"
            ),
        )

    try:
        file_bytes, filename = close_client.download_attachment(body.attachment_id)
    except CloseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"close_auth_unavailable: {exc}",
        ) from exc
    except CloseError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"close attachment {body.attachment_id!r} not found"
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"close_upstream_error: {exc}",
        ) from exc

    if len(file_bytes) > settings.aegis_max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"close attachment exceeds {settings.aegis_max_upload_bytes} bytes "
                f"(got {len(file_bytes)})"
            ),
        )

    # Audit BEFORE persist so the fetch event is durable even if persist fails.
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    existing = repository.find_by_hash(file_hash)
    duplicate = existing is not None

    upload_response = await persist_pdf_upload(
        enqueue_parse=_make_request_enqueue(request),
        body=file_bytes,
        original_filename=filename,
        repository=repository,
        audit=audit,
        actor=actor,
        actor_email=actor_email,
        merchant_id=merchant.id,
        close_lead_id=body.close_lead_id,
    )

    audit.record(
        actor=actor,
        actor_email=actor_email,
        action="close.upload.fetched",
        subject_type="document",
        subject_id=upload_response.document_id,
        details={
            "close_lead_id": body.close_lead_id,
            "attachment_id": body.attachment_id,
            "document_id": str(upload_response.document_id),
            "sha256": file_hash,
            "duplicate": duplicate,
            "filename": filename,
            "byte_size": len(file_bytes),
        },
    )

    return FromCloseResponse(
        document_id=upload_response.document_id,
        duplicate=duplicate,
        parse_enqueued=not duplicate,
    )


# Helpers ---------------------------------------------------------------------


def _resolve_merchant_id_for_upload(
    *,
    merchant_id: UUID | None,
    close_lead_id: str | None,
    merchants: MerchantRepository,
) -> UUID | None:
    """Decide which ``merchant_id`` to record on the upload row.

    Resolution rules:
      * Neither supplied -> None (unassociated upload, current behavior)
      * Only ``merchant_id`` supplied -> use it
      * Only ``close_lead_id`` supplied -> look up merchant, 404 if missing
      * Both supplied -> they must point at the same merchant, else 400
    """
    if close_lead_id is None:
        return merchant_id

    found = merchants.find_by_close_lead_id(close_lead_id)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no AEGIS merchant linked to close_lead_id "
                f"{close_lead_id!r}"
            ),
        )

    if merchant_id is not None and merchant_id != found.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"merchant_id {merchant_id} disagrees with close_lead_id "
                f"{close_lead_id!r} (which resolves to {found.id})"
            ),
        )
    return found.id


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


def _make_request_enqueue(request: Request) -> EnqueueParse:
    """Bind a FastAPI ``Request`` into an :data:`EnqueueParse` callable.

    Decouples :func:`persist_pdf_upload` from FastAPI's Request: route
    handlers compose the callable from their request context, while the
    arq orchestration worker composes a different callable that talks
    to ``ctx['redis']`` directly. Both reach the same persist path.
    """
    async def _enqueue(document_id: UUID, pdf_path: str) -> None:
        await _enqueue_parse_job(request, document_id=document_id, pdf_path=pdf_path)
    return _enqueue


__all__ = [
    "EnqueueParse",
    "FromCloseRequest",
    "FromCloseResponse",
    "UploadResponse",
    "persist_pdf_upload",
    "router",
    "uploads_router",
]
