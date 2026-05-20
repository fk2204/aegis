"""/funders — list, get, upsert, delete funders.

The matcher reads from the same repository, so a funder created here is
immediately visible to ``scoring/match_funders``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit, get_funder_repository, get_llm
from aegis.audit import AuditLog
from aegis.funders.extract import FunderExtractionError, extract_funder_guidelines
from aegis.funders.models import FunderGuidelineExtraction, FunderRow
from aegis.funders.repository import FunderNotFoundError, FunderRepository
from aegis.llm import LLMClient
from aegis.ops.operators import resolve_operator_email

router = APIRouter(
    prefix="/funders",
    tags=["funders"],
    dependencies=[Depends(require_bearer)],
)


@router.get("", response_model=list[FunderRow])
def list_funders(
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
) -> list[FunderRow]:
    return repo.list_active()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=FunderRow)
def create_funder(
    funder: FunderRow,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> FunderRow:
    try:
        saved = repo.upsert(funder)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit.record(
        actor="api",
        actor_email=actor_email,
        action="funder.create",
        subject_type="funder",
        subject_id=saved.id,
        details={"name": saved.name, "active": saved.active},
    )
    return saved


@router.get("/{funder_id}", response_model=FunderRow)
def get_funder(
    funder_id: UUID,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
) -> FunderRow:
    try:
        return repo.get(funder_id)
    except FunderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/{funder_id}", response_model=FunderRow)
def update_funder(
    funder_id: UUID,
    funder: FunderRow,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> FunderRow:
    if funder.id != funder_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="path id and body id must match",
        )
    try:
        saved = repo.upsert(funder)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit.record(
        actor="api",
        actor_email=actor_email,
        action="funder.update",
        subject_type="funder",
        subject_id=saved.id,
        details={"name": saved.name, "active": saved.active},
    )
    return saved


_MAX_FUNDER_PDF_BYTES = 25 * 1024 * 1024


@router.post(
    "/extract",
    response_model=FunderGuidelineExtraction,
    summary="Extract a draft FunderRow from a funder-criteria PDF.",
)
async def extract_funder(
    pdf: Annotated[UploadFile, File(description="Funder criteria PDF")],
    llm: Annotated[LLMClient, Depends(get_llm)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> FunderGuidelineExtraction:
    """Run the LLM extraction pass and return the draft + per-field confidence.

    The operator review/approve step is a separate call (``POST /funders``
    or ``PUT /funders/{id}``); this endpoint never persists.
    """
    body = await pdf.read(_MAX_FUNDER_PDF_BYTES + 1)
    if len(body) > _MAX_FUNDER_PDF_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"PDF exceeds {_MAX_FUNDER_PDF_BYTES} bytes",
        )
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="empty PDF"
        )
    try:
        extraction = extract_funder_guidelines(body, llm)
    except FunderExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    audit.record(
        actor="api",
        actor_email=actor_email,
        action="funder.extract",
        subject_type="funder",
        subject_id=extraction.draft.id,
        details={
            "draft_name": extraction.draft.name,
            "overall_confidence": extraction.overall_confidence,
            "low_confidence_fields": [
                k for k, v in extraction.confidence_by_field.items() if v < 60
            ],
        },
    )
    return extraction


@router.delete("/{funder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_funder(
    funder_id: UUID,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> None:
    repo.delete(funder_id)
    audit.record(
        actor="api",
        actor_email=actor_email,
        action="funder.delete",
        subject_type="funder",
        subject_id=funder_id,
    )


__all__ = ["router"]
