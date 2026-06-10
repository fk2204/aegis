"""Documents sub-router — statement detail page + aggregate drill-down.

Routes:
  * ``GET /ui/documents/{document_id}``                            — detail page
  * ``GET /ui/documents/{document_id}/aggregate/{aggregate}``      — HTMX partial
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_merchant_repository, get_repository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.storage import DocumentNotFoundError, DocumentRepository
from aegis.web._router_helpers import (
    _AGGREGATE_LABELS,
    _AGGREGATE_SOURCE_FIELDS,
)
from aegis.web._templates import templates

router = APIRouter()


@router.get(
    "/documents/{document_id}",
    response_class=HTMLResponse,
    summary="Statement detail — metadata + aggregates + every classified transaction.",
)
async def document_detail(
    request: Request,
    document_id: UUID,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    try:
        document = docs.get_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    analysis = docs.get_analysis(document_id)
    transactions = docs.list_transactions(document_id)

    merchant: MerchantRow | None = None
    if document.merchant_id is not None:
        try:
            merchant = merchants_repo.get(document.merchant_id)
        except MerchantNotFoundError:
            merchant = None

    # Category histogram for the in-page filter strip.
    category_counts: dict[str, int] = {}
    for t in transactions:
        category_counts[t.category] = category_counts.get(t.category, 0) + 1

    # Build a {tx_id -> set of aggregates that source-id back to it} map
    # so each row can show which aggregates it contributed to.
    contributes: dict[UUID, list[str]] = {}
    if analysis is not None:
        for agg, field in _AGGREGATE_SOURCE_FIELDS.items():
            for src_id in getattr(analysis, field, []):
                contributes.setdefault(src_id, []).append(_AGGREGATE_LABELS[agg])

    return templates.TemplateResponse(
        request,
        "document_detail.html.j2",
        {
            "document": document,
            "analysis": analysis,
            "transactions": transactions,
            "merchant": merchant,
            "category_counts": category_counts,
            "contributes": contributes,
            "aggregate_labels": _AGGREGATE_LABELS,
        },
    )


@router.get(
    "/documents/{document_id}/aggregate/{aggregate}",
    response_class=HTMLResponse,
    summary="HTMX partial — transactions that contributed to an aggregate.",
)
async def aggregate_drilldown(
    request: Request,
    document_id: UUID,
    aggregate: str,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
) -> HTMLResponse:
    if aggregate not in _AGGREGATE_SOURCE_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown aggregate: {aggregate!r}",
        )

    try:
        docs.get_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    analysis = docs.get_analysis(document_id)
    if analysis is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no analysis for document"
        )

    source_ids: list[UUID] = list(getattr(analysis, _AGGREGATE_SOURCE_FIELDS[aggregate]))
    all_txs = docs.list_transactions(document_id)
    contributing = [t for t in all_txs if t.id in set(source_ids)]

    return templates.TemplateResponse(
        request,
        "_transactions_partial.html.j2",
        {
            "transactions": contributing,
            "aggregate_label": _AGGREGATE_LABELS[aggregate],
        },
    )
