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
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aegis.api.deps import (
    get_funder_repository,
    get_llm,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.compliance.states import StateNotServed, validate_state_served
from aegis.funders.extract import FunderExtractionError, extract_funder_guidelines
from aegis.funders.models import FunderRow
from aegis.funders.repository import (
    FunderNotFoundError,
    FunderRepository,
)
from aegis.llm import LLMClient
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantConflictError,
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import FunderMatch, ScoreInput
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
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


@router.get("/review", response_class=HTMLResponse)
async def review_queue(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    """Manual-review queue — every document with parse_status=manual_review."""
    review_docs = docs.list_documents(parse_status="manual_review", limit=200)
    rows: list[dict[str, Any]] = []
    for doc in review_docs:
        merchant_label = "—"
        if doc.merchant_id is not None:
            try:
                m = merchants.get(doc.merchant_id)
                merchant_label = m.business_name
            except MerchantNotFoundError:
                merchant_label = f"merchant {str(doc.merchant_id)[:8]} (deleted)"
        rows.append(
            {
                "document_id": str(doc.id),
                "merchant_label": merchant_label,
                "uploaded_at": doc.uploaded_at.strftime("%Y-%m-%d %H:%M"),
                "fraud_score": doc.fraud_score if doc.fraud_score is not None else "—",
                "flags": doc.all_flags,
                "filename": doc.original_filename,
            }
        )
    return templates.TemplateResponse(request, "review.html.j2", {"rows": rows})


@router.get("/deals", response_class=HTMLResponse)
async def list_deals(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    """Deal lifecycle table.

    A "deal" is the derived join (merchant, latest document, latest analysis)
    per the Phase 7 audit decision. There is no ``deals`` table; this view
    enumerates merchants and shows their most recent document's parse status
    and analysis tier proxy. Merchants without any document show as
    ``Awaiting upload``.
    """
    rows: list[dict[str, Any]] = []
    for m in merchants_repo.list_all():
        latest_doc, latest_analysis = _find_latest_for_merchant(docs, m.id)
        rows.append(
            {
                "merchant_id": str(m.id),
                "business_name": m.business_name,
                "state": m.state,
                "uploaded_at": (
                    latest_doc.uploaded_at.strftime("%Y-%m-%d") if latest_doc else "—"
                ),
                "parse_status": latest_doc.parse_status if latest_doc else "no_upload",
                "fraud_score": (
                    latest_doc.fraud_score
                    if latest_doc and latest_doc.fraud_score is not None
                    else "—"
                ),
                "tier_proxy": _tier_proxy(latest_analysis),
                "document_id": str(latest_doc.id) if latest_doc else None,
            }
        )
    rows.sort(key=lambda r: r["uploaded_at"], reverse=True)
    return templates.TemplateResponse(request, "deals.html.j2", {"rows": rows})


@router.get("/merchants/new", response_class=HTMLResponse)
async def merchant_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "merchant_form.html.j2", {"merchant": None, "error": None}
    )


@router.post("/merchants/new", response_class=HTMLResponse, response_model=None)
async def merchant_new_submit(
    request: Request,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    business_name: Annotated[str, Form()],
    owner_name: Annotated[str, Form()],
    state: Annotated[str, Form()],
    dba: Annotated[str, Form()] = "",
    industry_naics: Annotated[str, Form()] = "",
    credit_score: Annotated[str, Form()] = "",
    time_in_business_months: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    error = _validate_merchant_state(state)
    if error is not None:
        return _merchant_form_error(request, error, _form_dict_from_locals(locals()))
    try:
        row = MerchantRow(
            business_name=business_name,
            owner_name=owner_name,
            state=state.upper(),
            dba=dba or None,
            industry_naics=industry_naics or None,
            credit_score=int(credit_score) if credit_score else None,
            time_in_business_months=int(time_in_business_months)
            if time_in_business_months
            else None,
            email=email or None,
            phone=phone or None,
        )
    except (ValueError, TypeError) as exc:
        return _merchant_form_error(request, str(exc), _form_dict_from_locals(locals()))
    try:
        saved = repo.upsert(row)
    except MerchantConflictError as exc:
        return _merchant_form_error(request, str(exc), _form_dict_from_locals(locals()))
    return RedirectResponse(f"/ui/merchants/{saved.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/merchants/{merchant_id}/edit", response_class=HTMLResponse)
async def merchant_edit_form(
    request: Request,
    merchant_id: UUID,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    try:
        merchant = repo.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request, "merchant_form.html.j2", {"merchant": merchant, "error": None}
    )


@router.post("/merchants/{merchant_id}/edit", response_class=HTMLResponse, response_model=None)
async def merchant_edit_submit(
    request: Request,
    merchant_id: UUID,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    business_name: Annotated[str, Form()],
    owner_name: Annotated[str, Form()],
    state: Annotated[str, Form()],
    dba: Annotated[str, Form()] = "",
    industry_naics: Annotated[str, Form()] = "",
    credit_score: Annotated[str, Form()] = "",
    time_in_business_months: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    try:
        existing = repo.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    error = _validate_merchant_state(state)
    if error is not None:
        return _merchant_form_error(
            request, error, _form_dict_from_locals(locals()), merchant=existing
        )
    try:
        updated = existing.model_copy(
            update={
                "business_name": business_name,
                "owner_name": owner_name,
                "state": state.upper(),
                "dba": dba or None,
                "industry_naics": industry_naics or None,
                "credit_score": int(credit_score) if credit_score else None,
                "time_in_business_months": int(time_in_business_months)
                if time_in_business_months
                else None,
                "email": email or None,
                "phone": phone or None,
            }
        )
    except (ValueError, TypeError) as exc:
        return _merchant_form_error(
            request, str(exc), _form_dict_from_locals(locals()), merchant=existing
        )
    try:
        saved = repo.upsert(updated)
    except MerchantConflictError as exc:
        return _merchant_form_error(
            request, str(exc), _form_dict_from_locals(locals()), merchant=existing
        )
    return RedirectResponse(f"/ui/merchants/{saved.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/funders", response_class=HTMLResponse)
async def list_funders_page(
    request: Request,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "funders.html.j2", {"funders": repo.list_active()}
    )


@router.get("/funders/import", response_class=HTMLResponse)
async def funder_import_form(request: Request) -> HTMLResponse:
    """Phase 7B: upload form for funder-criteria PDFs."""
    return templates.TemplateResponse(
        request, "funder_import.html.j2", {"error": None}
    )


_MAX_FUNDER_IMPORT_BYTES = 25 * 1024 * 1024


@router.post("/funders/import", response_class=HTMLResponse, response_model=None)
async def funder_import_review(
    request: Request,
    pdf: Annotated[UploadFile, File()],
    llm: Annotated[LLMClient, Depends(get_llm)],
) -> HTMLResponse:
    """Run the LLM extraction pass and render an editable review page.

    Stateless: the rendered form carries every field of the draft so the
    save endpoint receives the (possibly edited) values directly. Avoids
    a "drafts" table for Phase 7B.
    """
    body = await pdf.read(_MAX_FUNDER_IMPORT_BYTES + 1)
    if len(body) > _MAX_FUNDER_IMPORT_BYTES:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": f"PDF exceeds {_MAX_FUNDER_IMPORT_BYTES} bytes"},
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    if not body:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": "PDF was empty"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        extraction = extract_funder_guidelines(body, llm)
    except FunderExtractionError as exc:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    return templates.TemplateResponse(
        request,
        "funder_review.html.j2",
        {"extraction": extraction, "low_confidence_threshold": 60, "form_errors": []},
    )


@router.post("/funders/import/save", response_class=HTMLResponse, response_model=None)
async def funder_import_save(
    request: Request,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    name: Annotated[str, Form()],
    accepts_stacking: Annotated[str, Form()] = "false",
    min_monthly_revenue: Annotated[str, Form()] = "",
    min_avg_daily_balance: Annotated[str, Form()] = "",
    min_credit_score: Annotated[str, Form()] = "",
    min_months_in_business: Annotated[str, Form()] = "",
    max_positions: Annotated[str, Form()] = "",
    min_advance: Annotated[str, Form()] = "",
    max_advance: Annotated[str, Form()] = "",
    max_nsf_tolerance: Annotated[str, Form()] = "",
    typical_factor_low: Annotated[str, Form()] = "",
    typical_factor_high: Annotated[str, Form()] = "",
    typical_holdback_low: Annotated[str, Form()] = "",
    typical_holdback_high: Annotated[str, Form()] = "",
    excluded_industries: Annotated[str, Form()] = "",
    excluded_states: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Receive the reviewed/edited draft and upsert a FunderRow."""
    try:
        funder = FunderRow(
            name=name,
            accepts_stacking=accepts_stacking.lower() in {"true", "on", "yes", "1"},
            min_monthly_revenue=_decimal_or_none(min_monthly_revenue),
            min_avg_daily_balance=_decimal_or_none(min_avg_daily_balance),
            min_credit_score=_int_or_none(min_credit_score),
            min_months_in_business=_int_or_none(min_months_in_business),
            max_positions=_int_or_none(max_positions),
            min_advance=_decimal_or_none(min_advance),
            max_advance=_decimal_or_none(max_advance),
            max_nsf_tolerance=_int_or_none(max_nsf_tolerance),
            typical_factor_low=_decimal_or_none(typical_factor_low),
            typical_factor_high=_decimal_or_none(typical_factor_high),
            typical_holdback_low=_decimal_or_none(typical_holdback_low),
            typical_holdback_high=_decimal_or_none(typical_holdback_high),
            excluded_industries=tuple(
                s.strip() for s in excluded_industries.split(",") if s.strip()
            ),
            excluded_states=tuple(
                s.strip().upper() for s in excluded_states.split(",") if s.strip()
            ),
            notes=notes or None,
        )
    except (ValueError, TypeError) as exc:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": f"validation error: {exc}"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        saved = repo.upsert(funder)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": f"upsert failed: {exc}"},
            status_code=status.HTTP_409_CONFLICT,
        )
    return RedirectResponse(
        f"/ui/funders/{saved.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/funders/{funder_id}", response_class=HTMLResponse)
async def funder_detail(
    request: Request,
    funder_id: UUID,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
) -> HTMLResponse:
    try:
        funder = repo.get(funder_id)
    except FunderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request, "funder_detail.html.j2", {"funder": funder}
    )


@router.get("/merchants/{merchant_id}/match", response_class=HTMLResponse)
async def merchant_match(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> HTMLResponse:
    """Phase 7B matched-funders panel.

    Builds a ScoreInput from the merchant + latest analysis, scores it,
    iterates over active funders, and renders Centrex-style cards
    (eligible / soft-concerns / hard-fails). Operator picks via the API.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    latest_doc, analysis = _find_latest_for_merchant(docs, merchant_id)
    if latest_doc is None or analysis is None:
        return templates.TemplateResponse(
            request,
            "merchant_match.html.j2",
            {
                "merchant": merchant,
                "missing": "no_document",
                "score_result": None,
                "matches": [],
            },
        )

    score_input = _score_input_from_dashboard(merchant, latest_doc, analysis)
    try:
        score_result = score_deal(score_input, ofac=ofac)
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    cards: list[dict[str, Any]] = []
    for funder in funder_repo.list_active():
        m = match_funder(funder, score_input, score_result)
        if m is None:
            continue
        cards.append(_match_card(funder, m))
    cards.sort(key=lambda c: c["match_score"], reverse=True)

    return templates.TemplateResponse(
        request,
        "merchant_match.html.j2",
        {
            "merchant": merchant,
            "missing": None,
            "score_result": score_result,
            "matches": cards,
        },
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

    Uses the Protocol's ``list_documents`` filter so both the in-memory and
    Supabase repositories are exercised through the same indexed path.
    """
    rows = docs.list_documents(merchant_id=merchant_id, limit=1)
    if not rows:
        return None, None
    latest = rows[0]
    return latest, docs.get_analysis(latest.id)


def _decimal_or_none(value: str) -> Decimal | None:
    """Parse a form-string to Decimal; return None for empty/whitespace."""
    s = value.strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc


def _int_or_none(value: str) -> int | None:
    s = value.strip()
    if not s:
        return None
    return int(s)


def _score_input_from_dashboard(
    merchant: MerchantRow,
    document: DocumentRow,
    analysis: AnalysisRow,
) -> ScoreInput:
    """Build a ScoreInput for the matched-funders dashboard panel.

    The match panel needs the same shape ``score_deal`` consumes, but the
    dashboard's "deal" is a derived view (per audit F1) without the
    operator's requested-amount / requested-factor / requested-term-days
    inputs. Use sane defaults: midpoint of the funder typical ranges, 120-
    day term. Operator overrides via the bearer-token API path before any
    real submission ships.
    """
    monthly = _project_monthly(analysis.true_revenue, analysis.statement_days)
    return ScoreInput(
        merchant_id=merchant.id,
        business_name=merchant.business_name,
        owner_name=merchant.owner_name,
        state=merchant.state.upper(),
        industry_naics=merchant.industry_naics,
        industry_risk_tier=merchant.industry_risk_tier,
        time_in_business_months=merchant.time_in_business_months,
        credit_score=merchant.credit_score,
        avg_daily_balance=analysis.avg_daily_balance,
        true_revenue=analysis.true_revenue,
        monthly_revenue=monthly,
        lowest_balance=analysis.lowest_balance,
        num_nsf=analysis.num_nsf,
        days_negative=analysis.days_negative,
        mca_positions=analysis.mca_positions,
        mca_daily_total=analysis.mca_daily_total,
        debt_to_revenue=analysis.debt_to_revenue,
        payroll_detected=analysis.payroll_detected,
        returned_ach_count=analysis.returned_ach_count,
        statement_period_start=analysis.statement_period_start,
        statement_period_end=analysis.statement_period_end,
        statement_days=analysis.statement_days,
        fraud_score=document.fraud_score or 0,
        eof_markers=1,
        validation_passed=document.parse_status != "manual_review",
        extraction_confidence=100,
        # Operator-input fields not stored in analysis yet: use placeholders
        # that the funder match doesn't gate on (50K/1.30/120d). Real
        # submissions go through the API where these come from a form.
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _project_monthly(period_revenue: Decimal, statement_days: int) -> Decimal:
    if statement_days <= 0:
        return Decimal("0.00")
    return (period_revenue / Decimal(statement_days) * Decimal(30)).quantize(
        Decimal("0.01")
    )


def _match_card(funder: FunderRow, match: FunderMatch) -> dict[str, Any]:
    """Translate a FunderMatch into a card dict the template renders.

    ``match_funder`` sets ``match_score=0`` exactly when at least one hard
    fail fired (see ``_likelihood`` + the qualifies branch). When
    qualified, the ``soft_concerns`` list holds soft signals only; when
    not qualified it holds hard-fail reasons (matcher unions hard+soft so
    the operator sees the full picture). We split using ``match_score`` so
    the template can color-code without re-deriving the rule.

    Color rule:
      * red    — match_score == 0 (any hard fail)
      * yellow — match_score > 0 with at least one soft concern
      * green  — match_score > 0 and zero soft concerns
    """
    if match.match_score == 0:
        color = "red"
        hard_reasons = list(match.soft_concerns)
        soft_concerns: list[str] = []
    elif match.soft_concerns:
        color = "yellow"
        hard_reasons = []
        soft_concerns = list(match.soft_concerns)
    else:
        color = "green"
        hard_reasons = []
        soft_concerns = []

    return {
        "funder_id": str(funder.id),
        "funder_name": funder.name,
        "match_score": match.match_score,
        "color": color,
        "hard_reasons": hard_reasons,
        "soft_concerns": soft_concerns,
    }


def _validate_merchant_state(state: str) -> str | None:
    """Return an error string if the state isn't served, else None."""
    try:
        validate_state_served(state.upper())
    except StateNotServed as exc:
        return str(exc)
    return None


_FORM_FIELDS: tuple[str, ...] = (
    "business_name",
    "owner_name",
    "state",
    "dba",
    "industry_naics",
    "credit_score",
    "time_in_business_months",
    "email",
    "phone",
)


def _form_dict_from_locals(locs: dict[str, Any]) -> dict[str, str]:
    """Lift the named form fields out of a route's local namespace.

    Keeps the form re-render path strict: only the documented field names
    pass through, never auxiliary locals (request, repo, etc.) that would
    leak into the template context.
    """
    return {k: str(locs.get(k, "")) for k in _FORM_FIELDS}


def _merchant_form_error(
    request: Request,
    error: str,
    form: dict[str, str],
    *,
    merchant: MerchantRow | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "merchant_form.html.j2",
        {"merchant": merchant, "error": error, "form": form},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _tier_proxy(analysis: AnalysisRow | None) -> str:
    """Cheap tier hint for ``/ui/deals`` derived view.

    A real Tier comes from ``score_deal``; the lifecycle table doesn't run
    the scorer for every row (cost + side effects via OFAC). We surface a
    proxy from the parsed-analysis numbers — operators click into the deal
    detail to get the authoritative tier from the scoring API.
    """
    if analysis is None:
        return "—"
    if analysis.num_nsf >= 10 or analysis.days_negative > 15:
        return "F (proxy)"
    if analysis.mca_positions >= 2:
        return "F (proxy)"
    if analysis.num_nsf >= 5 or analysis.days_negative > 5:
        return "D/C (proxy)"
    if analysis.num_nsf >= 2:
        return "B (proxy)"
    return "A/B (proxy)"


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
