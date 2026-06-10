"""Upload sub-router — ad-hoc multi-file PDF upload form.

Routes:
  * ``GET /ui/upload``   — render the upload form (with merchant picker)
  * ``POST /ui/upload``  — multipart submit: 1..N files + optional ``merchant_id``

Behavior on ``POST``:
  * No merchant_id chosen → auto-create one provisional merchant per
    batch (Migration 034). Worker finalize fills the name from the
    parsed statement's ``account_holder``.
  * Empty merchant_id with no files → friendly HTML error.
  * Unknown merchant_id → friendly HTML error.
  * Files pass through ``_persist_uploads`` (shared helper) so every
    upload surface — this one, ``/ui/intake``, the bearer ``/upload``,
    ``/uploads/from-close`` — uses the same hash/dedup/persist path.

Extracted from ``router.py`` during R4.1. The shared upload helper
lives in ``aegis.web._router_helpers`` so both ``upload.py`` and
``intake.py`` consume it without re-importing the 4k-line aggregator.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.ops.operators import resolve_operator_email
from aegis.storage import DocumentRepository
from aegis.web._router_helpers import _persist_uploads
from aegis.web._templates import templates

router = APIRouter()


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "upload.html.j2",
        {
            "merchants": merchants_repo.list_all(),
            "results": None,
            "error": None,
            "merchant_just_uploaded": None,
        },
    )


@router.post("/upload", response_class=HTMLResponse, response_model=None)
async def upload_submit(
    request: Request,
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    merchant_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Browser-friendly multi-file upload — no bearer (Cloudflare Access in prod).

    Streams up to N PDFs in one multipart request, hashes/dedups each via
    the shared ``persist_pdf_upload`` helper, and renders an inline
    summary with each file's status. ``merchant_id`` is optional from
    this route — operators uploading ad-hoc may not have the merchant
    record yet (see ``/ui/intake`` for the combined create + upload flow).

    ``files`` is typed as ``| None`` so a browser submit with no file
    selected falls into the friendly HTML error branch below instead of
    bouncing off FastAPI's 422 validation gate as opaque JSON.
    """
    if not files or all(not f.filename for f in files):
        return templates.TemplateResponse(
            request,
            "upload.html.j2",
            {
                "merchants": merchants_repo.list_all(),
                "results": None,
                "error": "No files provided.",
                "merchant_just_uploaded": None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    parsed_merchant_id: UUID | None = None
    if merchant_id.strip():
        try:
            parsed_merchant_id = UUID(merchant_id.strip())
            merchants_repo.get(parsed_merchant_id)
        except (ValueError, MerchantNotFoundError):
            return templates.TemplateResponse(
                request,
                "upload.html.j2",
                {
                    "merchants": merchants_repo.list_all(),
                    "results": None,
                    "error": f"Unknown merchant_id {merchant_id!r}.",
                    "merchant_just_uploaded": None,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    else:
        # Migration 034 — auto-create branch (chunk B).
        #
        # The operator dropped files without picking a merchant. Create
        # ONE provisional merchant for the batch, attach every file to
        # it. Worker finalize at parse-completion fills the name from
        # ``statement.account_holder``; the failure paths flag the
        # merchant for manual naming so nothing zombies in
        # ``provisional`` forever.
        #
        # Scope (locked): this branch lives ONLY on the dashboard
        # ``/ui/upload``. The bearer ``/upload``, the Close-attachment
        # ``/uploads/from-close``, and the operator-curated
        # ``/ui/intake`` all keep their existing behavior (orphan,
        # Close-lead-resolved, and manual-create respectively).
        valid_files = [f for f in files if f.filename]
        if valid_files:
            provisional = merchants_repo.create_provisional()
            parsed_merchant_id = provisional.id
            audit.record(
                actor="dashboard",
                actor_email=actor_email,
                action="merchant.provisional_created",
                subject_type="merchant",
                subject_id=provisional.id,
                details={
                    "batch_size": len(valid_files),
                    "file_names": [f.filename for f in valid_files],
                    "uploaded_by": actor_email or "dashboard",
                },
            )

    settings = get_settings()
    results, total_error = await _persist_uploads(
        request=request,
        files=files,
        repository=repository,
        audit=audit,
        actor="dashboard",
        actor_email=actor_email,
        merchant_id=parsed_merchant_id,
        per_file_cap=settings.aegis_max_upload_bytes,
        total_cap=settings.aegis_max_intake_total_bytes,
    )

    # When at least one file landed on a specific merchant, surface that
    # merchant on the completion card so the broker can jump straight to
    # detail / match-funders without re-typing.
    merchant_just_uploaded: MerchantRow | None = None
    if parsed_merchant_id is not None and any(
        r.status in {"ok", "duplicate"} for r in results
    ):
        try:
            merchant_just_uploaded = merchants_repo.get(parsed_merchant_id)
        except MerchantNotFoundError:
            merchant_just_uploaded = None

    return templates.TemplateResponse(
        request,
        "upload.html.j2",
        {
            "merchants": merchants_repo.list_all(),
            "results": results,
            "error": total_error,
            "merchant_just_uploaded": merchant_just_uploaded,
        },
        status_code=(
            status.HTTP_200_OK if not total_error else status.HTTP_400_BAD_REQUEST
        ),
    )
