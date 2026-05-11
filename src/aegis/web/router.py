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

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_llm,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.api.routes.upload import persist_pdf_upload
from aegis.audit import AuditLog
from aegis.compliance.states import STATES, StateNotServed, validate_state_served
from aegis.config import get_settings
from aegis.funders.extract import FunderExtractionError, extract_funder_guidelines
from aegis.funders.models import FunderRow
from aegis.funders.repository import (
    FunderNotFoundError,
    FunderRepository,
)
from aegis.llm import LLMClient
from aegis.merchants.models import EntityType, MerchantRow
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
from aegis.web._stacking_card import build_stacking_card

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
async def upload_form(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "upload.html.j2",
        {"merchants": merchants_repo.list_all(), "results": None, "error": None},
    )


@router.post("/upload", response_class=HTMLResponse, response_model=None)
async def upload_submit(
    request: Request,
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
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
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    settings = get_settings()
    results, total_error = await _persist_uploads(
        request=request,
        files=files,
        repository=repository,
        audit=audit,
        actor="dashboard",
        merchant_id=parsed_merchant_id,
        per_file_cap=settings.aegis_max_upload_bytes,
        total_cap=settings.aegis_max_intake_total_bytes,
    )

    return templates.TemplateResponse(
        request,
        "upload.html.j2",
        {
            "merchants": merchants_repo.list_all(),
            "results": results,
            "error": total_error,
        },
        status_code=(
            status.HTTP_200_OK if not total_error else status.HTTP_400_BAD_REQUEST
        ),
    )


@router.get("/intake", response_class=HTMLResponse)
async def intake_form(request: Request) -> HTMLResponse:
    """Combined intake: create merchant + upload N statements in one POST."""
    return templates.TemplateResponse(
        request, "intake.html.j2", {"error": None, "form": {}}
    )


@router.post("/intake", response_class=HTMLResponse, response_model=None)
async def intake_submit(
    request: Request,
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    business_name: Annotated[str, Form()],
    owner_name: Annotated[str, Form()],
    state: Annotated[str, Form()],
    files: Annotated[list[UploadFile] | None, File()] = None,
    dba: Annotated[str, Form()] = "",
    industry_naics: Annotated[str, Form()] = "",
    credit_score: Annotated[str, Form()] = "",
    time_in_business_months: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    entity_type: Annotated[str, Form()] = "",
    ein: Annotated[str, Form()] = "",
    requested_amount: Annotated[str, Form()] = "",
    requested_factor: Annotated[str, Form()] = "",
    requested_term_days: Annotated[str, Form()] = "",
    broker_source: Annotated[str, Form()] = "",
    intake_date: Annotated[str, Form()] = "",
    is_renewal: Annotated[str, Form()] = "false",
) -> HTMLResponse | RedirectResponse:
    """Create merchant + upload N statements atomically.

    On any validation error the merchant is NOT created and the form
    re-renders with the entered values preserved. On success the
    operator lands on the merchant findings page with all uploaded
    documents already persisted.
    """
    form_payload: dict[str, Any] = {
        "business_name": business_name,
        "owner_name": owner_name,
        "state": state,
        "dba": dba,
        "industry_naics": industry_naics,
        "credit_score": credit_score,
        "time_in_business_months": time_in_business_months,
        "email": email,
        "phone": phone,
        "entity_type": entity_type,
        "ein": ein,
        "requested_amount": requested_amount,
        "requested_factor": requested_factor,
        "requested_term_days": requested_term_days,
        "broker_source": broker_source,
        "intake_date": intake_date,
        "is_renewal": is_renewal,
    }

    state_err = _validate_merchant_state(state)
    if state_err is not None:
        return _intake_form_error(request, state_err, form_payload)

    try:
        merchant = MerchantRow(
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
            entity_type=_entity_type_or_none(entity_type),
            ein=ein or None,
            requested_amount=Decimal(requested_amount) if requested_amount else None,
            requested_factor=Decimal(requested_factor) if requested_factor else None,
            requested_term_days=int(requested_term_days) if requested_term_days else None,
            broker_source=broker_source or None,
            intake_date=date.fromisoformat(intake_date) if intake_date else None,
            is_renewal=is_renewal.lower() in {"true", "on", "yes", "1"},
        )
    except (ValueError, TypeError) as exc:
        return _intake_form_error(request, str(exc), form_payload)
    try:
        merchant = merchants_repo.upsert(merchant)
    except MerchantConflictError as exc:
        return _intake_form_error(request, str(exc), form_payload)

    # Files are optional at intake — operator can create merchant first
    # and upload later.
    valid_files = [f for f in (files or []) if f.filename]
    if valid_files:
        settings = get_settings()
        results, total_error = await _persist_uploads(
            request=request,
            files=valid_files,
            repository=repository,
            audit=audit,
            actor="dashboard",
            merchant_id=merchant.id,
            per_file_cap=settings.aegis_max_upload_bytes,
            total_cap=settings.aegis_max_intake_total_bytes,
        )
        if total_error:
            # Merchant created but uploads failed; surface the error and
            # redirect to merchant detail so operator can retry uploads
            # individually from /ui/upload.
            audit.record(
                actor="dashboard",
                action="intake.partial_failure",
                subject_type="merchant",
                subject_id=merchant.id,
                details={"error": total_error, "results_count": len(results)},
            )

    return RedirectResponse(
        f"/ui/merchants/{merchant.id}", status_code=status.HTTP_303_SEE_OTHER
    )


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
    entity_type: Annotated[str, Form()] = "",
    ein: Annotated[str, Form()] = "",
    requested_amount: Annotated[str, Form()] = "",
    requested_factor: Annotated[str, Form()] = "",
    requested_term_days: Annotated[str, Form()] = "",
    broker_source: Annotated[str, Form()] = "",
    intake_date: Annotated[str, Form()] = "",
    is_renewal: Annotated[str, Form()] = "false",
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
            entity_type=_entity_type_or_none(entity_type),
            ein=ein or None,
            requested_amount=Decimal(requested_amount) if requested_amount else None,
            requested_factor=Decimal(requested_factor) if requested_factor else None,
            requested_term_days=int(requested_term_days) if requested_term_days else None,
            broker_source=broker_source or None,
            intake_date=date.fromisoformat(intake_date) if intake_date else None,
            is_renewal=is_renewal.lower() in {"true", "on", "yes", "1"},
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
    entity_type: Annotated[str, Form()] = "",
    ein: Annotated[str, Form()] = "",
    requested_amount: Annotated[str, Form()] = "",
    requested_factor: Annotated[str, Form()] = "",
    requested_term_days: Annotated[str, Form()] = "",
    broker_source: Annotated[str, Form()] = "",
    intake_date: Annotated[str, Form()] = "",
    is_renewal: Annotated[str, Form()] = "false",
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
                "entity_type": _entity_type_or_none(entity_type),
                "ein": ein or None,
                "requested_amount": Decimal(requested_amount) if requested_amount else None,
                "requested_factor": Decimal(requested_factor) if requested_factor else None,
                "requested_term_days": int(requested_term_days) if requested_term_days else None,
                "broker_source": broker_source or None,
                "intake_date": date.fromisoformat(intake_date) if intake_date else None,
                "is_renewal": is_renewal.lower() in {"true", "on", "yes", "1"},
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
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> HTMLResponse:
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    all_docs = docs.list_documents(merchant_id=merchant_id, limit=50)
    documents_table: list[dict[str, Any]] = []
    for d in all_docs:
        documents_table.append(
            {"document": d, "analysis": docs.get_analysis(d.id)}
        )

    latest_doc = all_docs[0] if all_docs else None
    latest_analysis = docs.get_analysis(latest_doc.id) if latest_doc else None

    score_result = None
    stacking = None
    if latest_doc is not None and latest_analysis is not None:
        score_input = _score_input_from_dashboard(merchant, latest_doc, latest_analysis)
        try:
            score_result = score_deal(score_input, ofac=ofac)
        except OFACStaleError:
            score_result = None
        stacking = build_stacking_card(
            latest_analysis, docs.list_transactions(latest_doc.id)
        )

    state_tier = _state_tier(merchant.state)
    ofac_status, ofac_match = _ofac_ribbon_status(ofac, merchant.business_name)

    from aegis.api.routes.findings import _compute_trend

    trend = _compute_trend(all_docs, docs)

    return templates.TemplateResponse(
        request,
        "merchant_detail.html.j2",
        {
            "merchant": merchant,
            "documents": documents_table,
            "document": latest_doc,
            "analysis": latest_analysis,
            "aggregate_labels": _AGGREGATE_LABELS,
            "score_result": score_result,
            "stacking": stacking,
            "state_tier": state_tier,
            "ofac_status": ofac_status,
            "ofac_match": ofac_match,
            "trend": trend,
        },
    )


@router.get("/merchants/{merchant_id}/findings.csv")
async def merchant_findings_csv(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> Response:
    """CSV download of the findings payload (no bearer; same trust model as the panel)."""
    from aegis.api.routes.findings import build_merchant_findings
    from aegis.web._findings_csv import findings_to_csv

    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    findings = build_merchant_findings(merchant=merchant, docs=docs, ofac=ofac)
    body = findings_to_csv(findings)
    filename = f"findings_{_slugify(merchant.business_name)}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
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


@dataclass
class _UploadResult:
    """Per-file outcome surfaced to the operator on the upload form."""

    filename: str
    status: str  # "ok" | "duplicate" | "error"
    document_id: str | None
    detail: str  # human-readable summary or error message


async def _persist_uploads(
    *,
    request: Request,
    files: list[UploadFile],
    repository: DocumentRepository,
    audit: AuditLog,
    actor: str,
    merchant_id: UUID | None,
    per_file_cap: int,
    total_cap: int,
) -> tuple[list[_UploadResult], str | None]:
    """Read N files, persist each via ``persist_pdf_upload``, return per-file
    outcomes plus an optional batch-level error.

    Per-file failures (oversize, non-PDF, dedup-race) become an entry in
    the result list with ``status="error"``; the batch keeps going so a
    bad file doesn't kill 3 good ones. A batch-level error (total cap
    exceeded) short-circuits and returns no results.
    """
    bodies: list[tuple[str, bytes]] = []
    running_total = 0
    for f in files:
        body = await f.read(per_file_cap + 1)
        if len(body) > per_file_cap:
            return (
                [],
                f"{f.filename or 'unnamed'} exceeds the per-file cap of {per_file_cap} bytes",
            )
        running_total += len(body)
        if running_total > total_cap:
            return (
                [],
                f"total upload size exceeds the {total_cap}-byte batch cap",
            )
        bodies.append((f.filename or "unnamed.pdf", body))

    results: list[_UploadResult] = []
    for filename, body in bodies:
        try:
            resp = await persist_pdf_upload(
                request=request,
                body=body,
                original_filename=filename,
                repository=repository,
                audit=audit,
                actor=actor,
                merchant_id=merchant_id,
            )
        except HTTPException as exc:
            results.append(
                _UploadResult(
                    filename=filename,
                    status="error",
                    document_id=None,
                    detail=str(exc.detail),
                )
            )
            continue
        results.append(
            _UploadResult(
                filename=filename,
                status="duplicate" if resp.duplicate_of_existing else "ok",
                document_id=str(resp.document_id),
                detail=(
                    "deduped to existing document"
                    if resp.duplicate_of_existing
                    else f"queued (parse_status={resp.parse_status})"
                ),
            )
        )
    return results, None


def _intake_form_error(
    request: Request, error: str, form: dict[str, Any]
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "intake.html.j2",
        {"error": error, "form": form},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


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
    "entity_type",
    "ein",
    "requested_amount",
    "requested_factor",
    "requested_term_days",
    "broker_source",
    "intake_date",
    "is_renewal",
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


def _slugify(text: str) -> str:
    """ASCII-safe slug for the CSV ``content-disposition`` filename."""
    out: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_") or "merchant"


def _entity_type_or_none(value: str) -> EntityType | None:
    """Coerce a form-string to ``EntityType`` or ``None``.

    Strict-cast: anything outside the literal set returns ``None`` so a
    mistyped entity_type doesn't crash the intake flow. Callers pass
    user input directly from the form, where the ``<select>`` constrains
    valid values, but defense-in-depth is cheap here.
    """
    v = value.strip().lower()
    if v in {"llc", "corp", "sole_prop", "partnership", "other"}:
        return cast(EntityType, v)
    return None


def _state_tier(state: str) -> int | str:
    """Resolve the state regulation tier for the ribbon.

    Returns 1/2/3 for served states; "unserved" if the state isn't in the
    served set. Pure read of ``STATES`` — no side effects.
    """
    reg = STATES.get(state.upper())
    if reg is None:
        return "unserved"
    return int(reg.tier)


def _ofac_ribbon_status(
    ofac: OFACClient | None, business_name: str
) -> tuple[str, bool | None]:
    """Best-effort OFAC indicator for the ribbon.

    Returns a (status, match) tuple where status is one of:
      * ``"checked"``  — query succeeded; ``match`` carries the boolean
      * ``"stale"``    — cache was stale and refresh failed
      * ``"unavailable"`` — query raised something else (treated as no info)
      * ``"not_consulted"`` — no client wired (dev/offline)
    """
    if ofac is None:
        return ("not_consulted", None)
    try:
        return ("checked", ofac.is_match(business_name))
    except OFACStaleError:
        return ("stale", None)
    except Exception:
        return ("unavailable", None)


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
