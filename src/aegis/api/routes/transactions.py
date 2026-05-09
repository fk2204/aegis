"""/documents/{id}/transactions — audit drill-down.

The dashboard loads this when the operator clicks an aggregate metric
(e.g. "Total Deposits: $47,300"). Filterable by category, posted-date
window, and source-page so a regulator's "show me where this came from"
question maps to a clickable link with page/line refs.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.api.auth import require_bearer
from aegis.api.deps import get_repository
from aegis.parser.models import ClassifiedTransaction
from aegis.storage import DocumentNotFoundError, DocumentRepository

router = APIRouter(
    prefix="/documents",
    tags=["transactions"],
    dependencies=[Depends(require_bearer)],
)


@router.get(
    "/{document_id}/transactions",
    response_model=list[ClassifiedTransaction],
    summary="List classified transactions for a document, with optional filters.",
)
def list_transactions(
    document_id: UUID,
    repo: Annotated[DocumentRepository, Depends(get_repository)],
    category: Annotated[
        str | None, Query(description="Filter to one transaction category")
    ] = None,
    date_from: Annotated[
        date | None, Query(description="Inclusive lower bound on posted_date")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Inclusive upper bound on posted_date")
    ] = None,
    page: Annotated[int | None, Query(ge=1, description="Source PDF page filter")] = None,
) -> list[ClassifiedTransaction]:
    try:
        repo.get_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    rows = repo.list_transactions(document_id, category=category)

    if date_from is not None:
        rows = [r for r in rows if r.posted_date >= date_from]
    if date_to is not None:
        rows = [r for r in rows if r.posted_date <= date_to]
    if page is not None:
        rows = [r for r in rows if r.source_page == page]
    return rows


__all__ = ["router"]
