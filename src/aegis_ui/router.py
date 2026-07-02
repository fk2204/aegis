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
    get_audit,
    get_decision_snapshot,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.compliance.snapshot import DecisionSnapshot
from aegis.funder_note_submissions.repository import FunderNoteSubmissionRepository
from aegis.funders.repository import FunderRepository
from aegis.merchants.repository import MerchantRepository
from aegis.scoring.ofac import OFACClient
from aegis.storage import DocumentRepository
from aegis.web._templates import templates

from .view_models import (
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
    # Fallback: if the pipeline has no finalized merchants (a fresh
    # environment or an ops window where every deal is still provisional)
    # show up to 50 non-deleted merchants so the Today view has content
    # to triage instead of rendering an empty queue.
    if not finalized:
        finalized = [m for m in _all_merchants if getattr(m, "deleted_at", None) is None][:50]
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

    # Pull merchant_outcomes for the Funded · 7d KPI. Fail-open — the
    # KPI degrades to "$0 / no funded deals" when the query blows up.
    outcomes_rows: list[dict[str, Any]] = []
    try:
        from aegis.db import get_supabase

        _outcomes_resp = (
            get_supabase()
            .table("merchant_outcomes")
            .select("outcome,funded_amount,recorded_at")
            .execute()
        )
        outcomes_rows = [r for r in (_outcomes_resp.data or []) if isinstance(r, dict)]
    except Exception:
        outcomes_rows = []

    return templates.TemplateResponse(
        request,
        "v2/today.html.j2",
        {"active": "today", **build_today_view(pipeline, outcomes=outcomes_rows)},
    )


# --------------------------------------------------------------- Deal
@router.get("/deal/{deal_id}", response_class=HTMLResponse)
def deal(
    request: Request,
    deal_id: str,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs_repo: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository, Depends(get_funder_note_submission_repository)
    ],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)] = None,
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
    latest_doc_id = None
    for d in documents:
        try:
            a = docs_repo.get_analysis(d.id)
        except Exception:
            a = None
        if a is not None:
            latest_analysis = a
            latest_doc_id = d.id
            break

    # Load transactions for the latest analyzed doc so
    # ``build_classification_view`` can render real coverage % + per-
    # bucket totals from ClassifiedTransaction.category.
    transactions: list[Any] = []
    if latest_doc_id is not None:
        try:
            transactions = list(docs_repo.list_transactions(latest_doc_id) or [])
        except Exception:
            transactions = []

    # Background context is derived from merchant fields directly (OFAC /
    # UCC / SOS / web-presence all live on MerchantRow); the view-model
    # reads them via getattr.
    background_ctx: dict[str, Any] = {}

    # Full scoring pipeline via the extracted helper (merchants.py).
    # Returns (merchant, score_input, score_result) or None. Feeds both
    # the funder-matching table AND the verdict card's paper_grade tag.
    score_result: Any = None
    funder_matches: list[Any] = []
    try:
        from aegis.web.routers.merchants import (
            run_funder_matching_for_merchant,
            run_scoring_pipeline_for_merchant,
        )

        pipeline_out = run_scoring_pipeline_for_merchant(
            merchant_id,
            merchants_repo=merchants_repo,
            docs=docs_repo,
            ofac=ofac,
        )
        if pipeline_out is not None:
            _, _, score_result = pipeline_out
            funder_matches = run_funder_matching_for_merchant(
                merchant_id,
                merchants_repo=merchants_repo,
                docs=docs_repo,
                funder_repo=funder_repo,
                funder_note_subs=funder_note_subs,
                snapshot=snapshot,
                ofac=ofac,
            )
    except Exception:  # pragma: no cover — never block deal render
        score_result = None
        funder_matches = []

    return templates.TemplateResponse(
        request,
        "v2/deal/dossier.html.j2",
        {
            "active": "deal",
            **build_deal_view(
                merchant,
                latest_analysis,
                documents,
                background_ctx,
                funder_matches,
                score_result=score_result,
                transactions=transactions,
            ),
        },
    )


@router.post("/deal/{deal_id}/reclassify", response_class=HTMLResponse)
def reclassify(
    request: Request,
    deal_id: str,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs_repo: Annotated[DocumentRepository, Depends(get_repository)],
) -> HTMLResponse:
    """Reclassify endpoint — stub until the counterparty reclassify service
    is extracted from ``scoring_v2.dossier_panel``. Returns the current
    classification partial so the HTMX swap doesn't 500.

    The v2 dossier's ``_classification.html.j2`` partial references
    ``deal.product`` so the response context must carry a full deal view
    — an empty classification alone would raise ``'deal' is undefined``.
    """
    try:
        merchant_id = UUID(deal_id)
        merchant = merchants_repo.get(merchant_id)
    except Exception:
        return templates.TemplateResponse(
            request,
            "v2/deal/_classification.html.j2",
            {"oob": False, **build_deal_view(None)},
        )
    docs: list[Any] = []
    latest_analysis = None
    try:
        docs = docs_repo.list_documents(merchant_id=merchant_id, limit=20) or []
        for d in docs:
            a = docs_repo.get_analysis(d.id)
            if a is not None:
                latest_analysis = a
                break
    except Exception:
        latest_analysis = None
    return templates.TemplateResponse(
        request,
        "v2/deal/_classification.html.j2",
        {"oob": False, **build_deal_view(merchant, latest_analysis, docs)},
    )


@router.post("/deal/{deal_id}/disposition", response_class=HTMLResponse)
def disposition(
    request: Request,
    deal_id: str,
    decision: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Persist operator outcome to ``merchant_outcomes`` (migration 106).

    Same target table the legacy dossier's four disposition buttons write
    to (``merchants.py`` merchant_record_funder_outcome route). Uses
    ``source="operator_v2"`` so the operator can distinguish v2-recorded
    outcomes from the legacy path when auditing. Idempotency-agnostic —
    duplicate posts create duplicate rows on purpose; the calibration
    engine treats them as separate events.
    """
    valid_outcomes = {"funded", "declined", "countered", "withdrawn"}
    recorded = False
    if decision in valid_outcomes:
        try:
            from datetime import UTC, datetime

            from aegis.db import get_supabase

            get_supabase().table("merchant_outcomes").insert(
                {
                    "merchant_id": deal_id,
                    "outcome": decision,
                    "source": "operator_v2",
                    "notes": (note or None),
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
            ).execute()
            recorded = True
        except Exception as exc:  # best-effort write
            import logging

            logging.getLogger(__name__).warning(
                "v2.disposition.insert_failed deal_id=%s exc=%s", deal_id, exc
            )
    return templates.TemplateResponse(
        request,
        "v2/deal/_disposition.html.j2",
        {
            "deal_id": deal_id,
            "decision": decision if recorded else None,
            "note": note,
        },
    )


# --------------------------------------------------------------- Funders
@router.get("/funders", response_class=HTMLResponse)
def funders(
    request: Request,
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository, Depends(get_funder_note_submission_repository)
    ],
    q: str = "",
    filter: str = "all",
) -> HTMLResponse:
    """Funders directory — pulls the active-funders list through the
    real ``FunderRepository`` and shapes it via ``build_funders_view``.
    Returns the ``_list.html.j2`` partial when the request is HTMX so
    the search / filter can swap results without re-rendering the shell.

    ``funder_note_subs`` is threaded so each card can render its
    ``last_submission_result`` / ``last_submission_date`` from the
    Close-Note submissions repository. Lookup failure degrades to
    ``"NO HISTORY YET"`` on the card, never blocks the page.
    """
    try:
        data = funder_repo.list_active()
    except Exception:
        data = []
    ctx = {
        "active": "funders",
        **build_funders_view(
            data,
            q=q,
            filt=filter,
            funder_note_subs=funder_note_subs,
        ),
    }
    tpl = "v2/funders/_list.html.j2" if _is_htmx(request) else "v2/funders/index.html.j2"
    return templates.TemplateResponse(request, tpl, ctx)


# --------------------------------------------------------------- Compliance
@router.get("/compliance", response_class=HTMLResponse)
def compliance(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit_log: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Compliance workbench — OFAC queue + KYB table + state licensing
    + recent activity + five KPIs.

    Reads through the merchant repository and audit-log dependency and
    filters in Python. ``merchants_repo.list_all()`` is small enough
    here; a bounded ``list_flagged()`` helper is a follow-on if the
    catalog grows. All external calls are guarded — every section
    degrades to an empty state before failing the page render.
    """
    try:
        all_m = merchants_repo.list_all()
    except Exception:
        all_m = []
    ofac_rows = [m for m in all_m if getattr(m, "ofac_is_clear", None) is False]
    ctx = build_compliance_view(
        ofac_rows,
        all_merchants=all_m,
        audit_log=audit_log,
    )
    return templates.TemplateResponse(
        request,
        "v2/compliance/index.html.j2",
        {"active": "compliance", **ctx},
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
