"""/funders — list, get, upsert, delete funders.

The matcher reads from the same repository, so a funder created here is
immediately visible to ``scoring/match_funders``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit, get_funder_repository
from aegis.audit import AuditLog
from aegis.funders.models import FunderRow
from aegis.funders.repository import FunderNotFoundError, FunderRepository

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
) -> FunderRow:
    try:
        saved = repo.upsert(funder)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit.record(
        actor="api",
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
        action="funder.update",
        subject_type="funder",
        subject_id=saved.id,
        details={"name": saved.name, "active": saved.active},
    )
    return saved


@router.delete("/{funder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_funder(
    funder_id: UUID,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> None:
    repo.delete(funder_id)
    audit.record(
        actor="api",
        action="funder.delete",
        subject_type="funder",
        subject_id=funder_id,
    )


__all__ = ["router"]
