"""aegis_ui/router.py — wired to real AEGIS data (Step 4 of migration).

Additive /v2 UI. Every legacy route stays untouched. Data loads through
the exact repositories the legacy dossier already uses — no new query
paths, no direct table access, no re-implementations of scoring.

Repository layer:
  * ``get_repository()``          -> DocumentRepository
  * ``get_merchant_repository()`` -> MerchantRepository
  * ``get_funder_repository()``   -> FunderRepository
Real Pydantic models (``MerchantRow``, ``AnalysisRow``, ``DocumentRow``,
``FunderRow``, ``FunderMatch``) flow all the way to the view-model layer;
no ``type("M", (), row.data[0])()`` monkey-shim needed.

Funder-matching orchestration lives inside the legacy dossier's
``merchant_detail`` and hasn't been extracted into a service yet, so the
v2 deal page ships without live funder pricing on this PR — pricing rows
will fall back to the empty state until the extraction lands.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_funder_repository,
    get_merchant_repository,
    get_repository,
)
from aegis.funders.repository import FunderRepository
from aegis.merchants.repository import MerchantRepository
from aegis.storage import DocumentRepository
from aegis.web._templates import templates

from .view_models import (
    build_classification_view,
    build_compliance_view,
    build_deal_view,
    build_funders_view,
    build_today_view,
)

router = APIRouter(prefix="/v2", tags=["aegis-ui-v2"])


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# --------------------------------------------------------------- Today
@router.get("/", response_class=HTMLResponse)
def today(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs_repo: Annotated[DocumentRepository, Depends(get_repository)],
) -> HTMLResponse:
    """Today — pipeline snapshot across the last N merchants.

    Loads up to 50 finalized merchants + their documents through the
    repository layer (no direct Supabase table access), and joins each
    to the freshest AnalysisRow the DocumentRepository can produce for
    that merchant's most-recent parse.
    """
    from aegis.merchants.models import MerchantStatus

    _all_merchants = merchants_repo.list_all()
    finalized: list[Any] = [m for m in _all_merchants if getattr(m, "status", None) == "finalized"][
        :50
    ]
    del MerchantStatus  # future: filter by broader status set here

    pipeline: list[dict[str, Any]] = []
    for m in finalized:
        try:
            docs = docs_repo.list_documents(merchant_id=m.id, limit=10) or []
        except Exception:  # never block /v2 on a per-merchant repo blip
            docs = []
        latest_analysis = None
        for d in docs:
            try:
                a = docs_repo.get_analysis(d.id)
            except Exception:
                a = None
            if a is not None:
                latest_analysis = a
                break
        pipeline.append({"merchant": m, "analysis": latest_analysis, "documents": docs})

    return templates.TemplateResponse(
        request,
        "v2/today.html.j2",
        {"active": "today", **build_today_view(pipeline)},
    )


# --------------------------------------------------------------- Deal
@router.get("/deal/{deal_id}", response_class=HTMLResponse)
def deal(
    request: Request,
    deal_id: str,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs_repo: Annotated[DocumentRepository, Depends(get_repository)],
) -> HTMLResponse:
    """Deal dossier — v2 skeleton with real merchant + latest analysis + docs.

    Renders the empty-state deal view when ``deal_id`` is malformed or
    the merchant can't be resolved (never 500s).
    """
    try:
        merchant_id = UUID(deal_id)
    except ValueError:
        return templates.TemplateResponse(
            request, "v2/deal/dossier.html.j2", {"active": "deal", **build_deal_view(None)}
        )

    try:
        merchant = merchants_repo.get(merchant_id)
    except Exception:
        return templates.TemplateResponse(
            request, "v2/deal/dossier.html.j2", {"active": "deal", **build_deal_view(None)}
        )

    try:
        documents = docs_repo.list_documents(merchant_id=merchant_id, limit=20) or []
    except Exception:
        documents = []

    latest_analysis = None
    for d in documents:
        try:
            a = docs_repo.get_analysis(d.id)
        except Exception:
            a = None
        if a is not None:
            latest_analysis = a
            break

    # Background context is derived from merchant fields directly (OFAC /
    # UCC / SOS / web-presence all live on MerchantRow); the view-model
    # reads them via getattr.
    background_ctx: dict[str, Any] = {}

    # Funder matches deferred — the orchestrator that runs matching is
    # embedded in the legacy dossier route and hasn't been extracted into
    # a service layer this router can call cheaply. Pricing surfaces as
    # empty-state until that extraction lands.
    funder_matches: list[Any] | None = None

    return templates.TemplateResponse(
        request,
        "v2/deal/dossier.html.j2",
        {
            "active": "deal",
            **build_deal_view(merchant, latest_analysis, documents, background_ctx, funder_matches),
        },
    )


@router.post("/deal/{deal_id}/reclassify", response_class=HTMLResponse)
def reclassify(request: Request, deal_id: str) -> HTMLResponse:
    """Reclassify endpoint — stub until the real reclassify service exists.

    Returns the classification partial with empty state so the HTMX swap
    doesn't 500.
    """
    _ = deal_id  # reserved
    return templates.TemplateResponse(
        request,
        "v2/deal/_classification.html.j2",
        {"oob": False, **build_classification_view(None)},
    )


@router.post("/deal/{deal_id}/disposition", response_class=HTMLResponse)
def disposition(
    request: Request,
    deal_id: str,
    decision: str = Form(...),
    note: str = Form(""),
) -> HTMLResponse:
    """Disposition endpoint — stub. Real save-path lives in the legacy
    dossier route; wiring pending extraction.
    """
    _ = (deal_id, decision, note)
    return templates.TemplateResponse(
        request, "v2/deal/dossier.html.j2", {"active": "deal", **build_deal_view(None)}
    )


# --------------------------------------------------------------- Funders
@router.get("/funders", response_class=HTMLResponse)
def funders(
    request: Request,
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    q: str = "",
    filter: str = "all",
) -> HTMLResponse:
    """Funders directory — pulls the active-funders list through the
    real ``FunderRepository`` and shapes it via ``build_funders_view``.
    Returns the ``_list.html.j2`` partial when the request is HTMX so
    the search input can swap results without re-rendering the shell.
    """
    try:
        data = funder_repo.list_active()
    except Exception:
        data = []
    ctx = {"active": "funders", **build_funders_view(data, q=q, filt=filter)}
    tpl = "v2/funders/_list.html.j2" if _is_htmx(request) else "v2/funders/index.html.j2"
    return templates.TemplateResponse(request, tpl, ctx)


# --------------------------------------------------------------- Compliance
@router.get("/compliance", response_class=HTMLResponse)
def compliance(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    """Compliance workbench — surfaces the OFAC-blocked merchant queue.

    Reads through the merchant repository and filters in Python
    (``merchants_repo.list_all()`` is small enough here; a bounded
    ``list_flagged()`` helper is a follow-on).
    """
    try:
        all_m = merchants_repo.list_all()
    except Exception:
        all_m = []
    ofac_rows: list[dict[str, Any]] = []
    for m in all_m:
        if getattr(m, "ofac_is_clear", None) is False:
            ofac_rows.append(
                {
                    "id": str(m.id),
                    "business_name": m.business_name,
                    "state": m.state,
                    "ofac_match_detail": list(getattr(m, "ofac_match_detail", []) or []),
                }
            )
    return templates.TemplateResponse(
        request,
        "v2/compliance/index.html.j2",
        {"active": "compliance", **build_compliance_view(ofac_rows)},
    )


@router.post("/compliance/ofac/{deal_id}/{action}", response_class=HTMLResponse)
def ofac_action(request: Request, deal_id: str, action: str) -> HTMLResponse:
    """OFAC card action endpoint — stub until the real
    clear/escalate service exists. Returns an empty card partial.
    """
    _ = (deal_id, action)
    return templates.TemplateResponse(
        request,
        "v2/compliance/index.html.j2",
        {"active": "compliance", **build_compliance_view(None)},
    )
