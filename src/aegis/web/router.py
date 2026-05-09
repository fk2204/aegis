"""Operator dashboard routes.

Five pages + one HTMX partial:

  * ``GET /ui/``                              — index with summary tiles
  * ``GET /ui/upload``                        — upload form (POSTs to /upload)
  * ``GET /ui/merchants``                     — table of all merchants
  * ``GET /ui/merchants/{id}``                — merchant detail with aggregates
  * ``GET /ui/documents/{id}/aggregate/{name}``  — HTMX partial: drill-down
    transactions for one aggregate. Returned as HTML fragment so HTMX
    can swap into the detail page.

Auth note
---------
The dashboard intentionally does NOT require the bearer token: in
production it sits behind Cloudflare Access (SSO + JWT). The bearer
token guards programmatic API endpoints, not the operator UI. In a
local dev box without Cloudflare in front, the dashboard is reachable
on localhost only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aegis.api.deps import get_merchant_repository, get_repository
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.parser.models import ClassifiedTransaction
from aegis.storage import (
    AnalysisRow,
    DocumentNotFoundError,
    DocumentRepository,
    DocumentRow,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(prefix="/ui", tags=["dashboard"])


_AGGREGATE_LABELS: dict[str, str] = {
    "true_revenue": "True Revenue",
    "avg_daily_balance": "Average Daily Balance",
    "num_nsf": "NSF Count",
    "days_negative": "Days Negative",
    "mca_daily_total": "MCA Daily Total",
}

_AGGREGATE_SOURCE_FIELDS: dict[str, str] = {
    "true_revenue": "true_revenue_source_ids",
    "avg_daily_balance": "avg_daily_balance_source_ids",
    "num_nsf": "num_nsf_source_ids",
    "days_negative": "days_negative_source_ids",
    "mca_daily_total": "mca_daily_total_source_ids",
}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html.j2", {"now": datetime.now(UTC).isoformat()}
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "upload.html.j2", {})


@router.get("/merchants", response_class=HTMLResponse)
async def list_merchants(
    request: Request,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "merchants.html.j2", {"merchants": repo.list_all()}
    )


@router.get("/merchants/{merchant_id}", response_class=HTMLResponse)
async def merchant_detail(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
) -> HTMLResponse:
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    latest_doc, latest_analysis = _find_latest_for_merchant(docs, merchant_id)
    return templates.TemplateResponse(
        request,
        "merchant_detail.html.j2",
        {
            "merchant": merchant,
            "document": latest_doc,
            "analysis": latest_analysis,
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


# Helpers --------------------------------------------------------------------


def _find_latest_for_merchant(
    docs: DocumentRepository,
    merchant_id: UUID,
) -> tuple[DocumentRow | None, AnalysisRow | None]:
    """Return the most recently uploaded document + analysis for a merchant.

    The in-memory repository doesn't index by merchant; we walk the dict
    once. The Supabase implementation will replace this with a single
    indexed query.
    """
    candidates: list[DocumentRow] = []
    seen = getattr(docs, "_docs", None)
    if seen is None:
        return None, None
    for doc in seen.values():
        if doc.merchant_id == merchant_id:
            candidates.append(doc)
    if not candidates:
        return None, None
    latest = max(candidates, key=lambda d: d.uploaded_at)
    return latest, docs.get_analysis(latest.id)


def _txs_to_rows(txs: list[ClassifiedTransaction]) -> list[dict[str, Any]]:
    """Stable display ordering by posted_date, then page/line."""
    return [
        {
            "posted_date": t.posted_date.isoformat(),
            "description": t.description,
            "amount": str(t.amount),
            "running_balance": str(t.running_balance) if t.running_balance else "",
            "category": t.category,
            "source_page": t.source_page,
            "source_line": t.source_line,
        }
        for t in sorted(txs, key=lambda t: (t.posted_date, t.source_page, t.source_line))
    ]


__all__ = ["router"]
