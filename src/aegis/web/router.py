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

import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aegis.api.deps import (
    get_audit,
    get_funder_reply_repository,
    get_funder_repository,
    get_llm,
    get_merchant_repository,
    get_ofac_client,
    get_override_repository,
    get_repository,
)
from aegis.api.routes.upload import persist_pdf_upload
from aegis.audit import AuditLog
from aegis.compliance.overrides import (
    OverrideError,
    OverridePayload,
    OverrideRepository,
    record_override,
)
from aegis.compliance.states import STATES, StateNotServed, validate_state_served
from aegis.config import get_settings
from aegis.funders.extract import FunderExtractionError, extract_funder_guidelines
from aegis.funders.models import FunderRow
from aegis.funders.replies import FunderReplyRepository
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
from aegis.parser.patterns import analyze_patterns
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import FunderMatch, ScoreInput
from aegis.scoring.multi_month import (
    detect_missing_months as _detect_missing_months,
)
from aegis.scoring.multi_month import (
    score_input_multi_month as _score_input_multi_month,
)
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
from aegis.scoring.submission_package import build_submission_files
from aegis.storage import (
    AnalysisRow,
    DocumentNotFoundError,
    DocumentRepository,
    DocumentRow,
)
from aegis.web._pattern_cards import build_pattern_cards
from aegis.web._slug import slugify
from aegis.web._soft_signals import parse_soft_signal_flags
from aegis.web._stacking_card import build_stacking_card

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Jinja filters accept arbitrary template-side values (None, Decimal, int,
# str). The unions below cover what AEGIS actually sends through — typing
# more narrowly would force callers to pre-coerce, defeating the filter's
# purpose. Justifies the broad input types per CLAUDE.md "Any" rule.
_MoneyLike = Decimal | int | float | str | None
_NumericLike = int | str | None


def _money_filter(value: _MoneyLike, *, whole: bool = False) -> str:
    """Format a Decimal/int/float as $X,XXX[.XX]. None → em-dash."""
    if value is None or value == "":
        return "—"
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value)
    sign = "-" if d < 0 else ""
    d = abs(d)
    if whole or d == d.to_integral_value():
        whole_part = int(d)
        return f"{sign}${whole_part:,}"
    cents = d.quantize(Decimal("0.01"))
    int_part, _, frac = str(cents).partition(".")
    return f"{sign}${int(int_part):,}.{frac}"


def _whole_money_filter(value: _MoneyLike) -> str:
    return _money_filter(value, whole=True)


def _days_label_filter(value: _NumericLike) -> str:
    if value is None or value == "":
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{n} day" if n == 1 else f"{n} days"


def _fraud_band(score: _NumericLike) -> str:
    """Map fraud_score 0-100 to a risk band keyed off pipeline.py thresholds.

    Bands mirror parser.pipeline constants exactly: REVIEW_THRESHOLD=35,
    HARD_DECLINE_THRESHOLD=65. Keeps UI legend in sync with parse_status gate.
    """
    if score is None:
        return "unknown"
    try:
        n = int(score)
    except (TypeError, ValueError):
        return "unknown"
    if n < 35:
        return "clear"
    if n < 65:
        return "review"
    return "decline"


templates.env.filters["money"] = _money_filter
templates.env.filters["whole_money"] = _whole_money_filter
templates.env.filters["days_label"] = _days_label_filter
templates.env.filters["fraud_band"] = _fraud_band

router = APIRouter(prefix="/ui", tags=["dashboard"])


_AGGREGATE_LABELS: dict[str, str] = {
    "true_revenue": "True Revenue",
    "avg_daily_balance": "Average Daily Balance",
    "num_nsf": "NSF Count",
    "days_negative": "Days Negative",
    "mca_daily_total": "MCA Daily Total",
}

# Per-aggregate unit hint shown under the KPI value (e.g. "$" amount,
# "days", "count"). Kept aligned with _AGGREGATE_LABELS — every key
# present in labels must have an entry here so the KPI tile can format.
_AGGREGATE_UNIT_KIND: dict[str, str] = {
    "true_revenue": "money",
    "avg_daily_balance": "money",
    "num_nsf": "count",
    "days_negative": "days",
    "mca_daily_total": "money",
}

_AGGREGATE_SOURCE_FIELDS: dict[str, str] = {
    "true_revenue": "true_revenue_source_ids",
    "avg_daily_balance": "avg_daily_balance_source_ids",
    "num_nsf": "num_nsf_source_ids",
    "days_negative": "days_negative_source_ids",
    "mca_daily_total": "mca_daily_total_source_ids",
}


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Today dashboard — live KPIs sourced from Supabase / in-memory repos.

    Funnel: parse-status histogram with proportional bar widths so the
    operator sees how many docs are at each stage. "Sent to funders"
    and "Funded" are sourced from the audit_log action histogram (no
    table for these yet — Phase 7C work).

    Attention queue: most-recent ``manual_review`` documents joined to
    their merchant for context.

    Recent activity: last 10 ``audit_log`` rows. The audit table is
    masked at write time so PII never lands here either.
    """
    parse_counts = docs.count_by_parse_status()
    merchant_total = merchants_repo.count_total()

    proceed = parse_counts.get("proceed", 0)
    review = parse_counts.get("review", 0)
    manual_review = parse_counts.get("manual_review", 0)
    pending = parse_counts.get("pending", 0)
    error = parse_counts.get("error", 0)
    parsed_total = proceed + review + manual_review + error
    in_pipeline = parsed_total + pending

    recent_activity_rows = audit.list_recent(limit=10)
    recent_activity = [
        {
            "actor": r.get("actor") or "—",
            "action": r.get("action") or "—",
            "subject_type": r.get("subject_type") or "",
            "subject_id": r.get("subject_id") or "",
            "time_short": _format_activity_time(r.get("created_at")),
        }
        for r in recent_activity_rows
    ]

    attention = []
    for d in docs.list_documents(parse_status="manual_review", limit=8):
        merchant_label = "—"
        if d.merchant_id is not None:
            try:
                merchant_label = merchants_repo.get(d.merchant_id).business_name
            except MerchantNotFoundError:
                merchant_label = f"merchant {str(d.merchant_id)[:8]}"
        attention.append(
            {
                "document_id": str(d.id),
                "merchant_label": merchant_label,
                "fraud_score": d.fraud_score if d.fraud_score is not None else "—",
                "uploaded_at": d.uploaded_at.strftime("%Y-%m-%d %H:%M"),
                "flags": "; ".join(d.all_flags) if d.all_flags else "",
            }
        )

    submitted_count = sum(
        1 for r in recent_activity_rows if r.get("action") == "deal.submit_to_funders"
    )
    funded_count = sum(
        1 for r in recent_activity_rows if r.get("action") == "deal.funded"
    )

    funnel_rows = _build_funnel_rows(
        intake_count=merchant_total,
        docs_uploaded=in_pipeline,
        parsed=parsed_total,
        underwritten=proceed + review,
        submitted=submitted_count,
        funded=funded_count,
        declined=manual_review + error,
    )

    return templates.TemplateResponse(
        request,
        "index.html.j2",
        {
            "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "merchant_total": merchant_total,
            "in_pipeline": in_pipeline,
            "manual_review_count": manual_review,
            "proceed_count": proceed,
            "review_count": review,
            "pending_count": pending,
            "error_count": error,
            "funnel_rows": funnel_rows,
            "attention": attention,
            "recent_activity": recent_activity,
        },
    )


def _build_funnel_rows(
    *,
    intake_count: int,
    docs_uploaded: int,
    parsed: int,
    underwritten: int,
    submitted: int,
    funded: int,
    declined: int,
) -> list[dict[str, Any]]:
    """Compute proportional bar widths for the pipeline funnel.

    Width is relative to ``intake_count`` (the widest bar). All other
    stages are smaller-or-equal. Empty pipeline collapses to zero-width
    bars rather than rendering garbage.
    """
    base = max(intake_count, 1)

    def _w(n: int) -> int:
        return min(100, int((n / base) * 100)) if base > 0 else 0

    return [
        {"label": "Intake", "count": intake_count, "width": _w(intake_count), "cls": ""},
        {"label": "Docs uploaded", "count": docs_uploaded, "width": _w(docs_uploaded), "cls": ""},
        {"label": "Parsed", "count": parsed, "width": _w(parsed), "cls": "accent"},
        {"label": "Underwritten", "count": underwritten, "width": _w(underwritten), "cls": ""},
        {"label": "Sent to funders", "count": submitted, "width": _w(submitted), "cls": "pos"},
        {"label": "Funded", "count": funded, "width": _w(funded), "cls": "pos"},
        {"label": "Declined", "count": declined, "width": _w(declined), "cls": "neg"},
    ]


def _format_activity_time(value: object) -> str:
    """Render an audit_log ``created_at`` value for the dashboard timeline."""
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            return value[:16]
    return "—"


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
    docs_uploaded = 0
    docs_failed = 0
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
        docs_uploaded = sum(1 for r in results if r.status in {"ok", "duplicate"})
        docs_failed = sum(1 for r in results if r.status == "error")
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

    # Carry a "just created" flash to the merchant detail page so it can
    # render a confirmation banner with upload count + next-step CTAs.
    target = (
        f"/ui/merchants/{merchant.id}"
        f"?from_intake=1&docs={docs_uploaded}"
        f"{'&failed=' + str(docs_failed) if docs_failed else ''}"
    )
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


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
    merchants = list(merchants_repo.list_all())

    # Batch fetch the latest document per merchant. Repository returns
    # documents most-recent-first; deduping by merchant_id and keeping
    # the first occurrence yields each merchant's latest. Then one
    # batch analyses fetch covers all of those documents. Total: 2
    # queries regardless of merchant count (was 2N).
    all_docs = docs.list_documents(limit=500)
    latest_by_merchant: dict[UUID, DocumentRow] = {}
    for d in all_docs:
        if d.merchant_id is None or d.merchant_id in latest_by_merchant:
            continue
        latest_by_merchant[d.merchant_id] = d

    analyses_by_doc = docs.get_analyses_by_document_ids(
        [d.id for d in latest_by_merchant.values()]
    )

    rows: list[dict[str, Any]] = []
    for m in merchants:
        latest_doc = latest_by_merchant.get(m.id)
        latest_analysis = (
            analyses_by_doc.get(latest_doc.id) if latest_doc is not None else None
        )
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
    audit: Annotated[AuditLog, Depends(get_audit)],
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

    items = _collect_analyzed_for_merchant(docs, merchant_id)
    if not items:
        return templates.TemplateResponse(
            request,
            "merchant_match.html.j2",
            {
                "merchant": merchant,
                "missing": "no_document",
                "score_result": None,
                "matches": [],
                "score_window": None,
            },
        )

    score_input = _score_input_multi_month(merchant, items)
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
        cards.append(_match_card(funder, m, score_input))
    cards.sort(key=lambda c: c["match_score"], reverse=True)

    score_window = {
        "months_used": len(items),
        "period_start": score_input.statement_period_start,
        "period_end": score_input.statement_period_end,
        "any_manual_review": any(
            d.parse_status == "manual_review" for d, _ in items
        ),
    }

    funder_responses = _latest_funder_responses(audit, merchant_id)

    return templates.TemplateResponse(
        request,
        "merchant_match.html.j2",
        {
            "merchant": merchant,
            "missing": None,
            "score_result": score_result,
            "matches": cards,
            "score_window": score_window,
            "funder_responses": funder_responses,
        },
    )


@router.post("/merchants/{merchant_id}/submit", response_model=None)
async def merchant_submit_to_funders(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    funder_ids: Annotated[list[str], Form()],
) -> Response:
    """Build per-funder submission CSVs and stream them as a ZIP.

    Operator-triggered from the match panel. ``funder_ids`` is the
    multi-select of funder UUIDs the operator chose to forward to.
    A single funder returns the CSV inline; multiple funders return a
    ZIP. Always audits ``deal.submit_to_funders`` regardless of count.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    requested_ids = _parse_funder_ids(funder_ids)
    if not requested_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no funders selected",
        )

    items = _collect_analyzed_for_merchant(docs, merchant_id)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no analyzed document — upload + parse first",
        )

    score_input = _score_input_multi_month(merchant, items)
    try:
        score_result = score_deal(score_input, ofac=ofac)
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    requested_set = set(requested_ids)
    matched: list[FunderMatch] = []
    for f in funder_repo.list_active():
        if f.id not in requested_set:
            continue
        m = match_funder(f, score_input, score_result)
        if m is None:
            continue
        matched.append(m)

    if not matched:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="none of the selected funders have configured criteria for this merchant",
        )

    files = build_submission_files(score_input, score_result, matched)

    merchant_slug = slugify(merchant.business_name)
    if len(files) == 1:
        only = files[0]
        download_bytes = only.csv_bytes
        download_filename = only.filename
        download_media = "text/csv; charset=utf-8"
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for sub in files:
                zf.writestr(sub.filename, sub.csv_bytes)
        download_bytes = buf.getvalue()
        download_filename = f"submission_{merchant_slug}.zip"
        download_media = "application/zip"

    # Render the PDF dossier for Zoho attachment. WeasyPrint native libs
    # ship on the Hetzner box; on Windows dev they're absent and we log
    # the OSError + continue without a PDF. The submission flow MUST
    # complete even if PDF rendering fails.
    dossier_pdf, dossier_filename = _maybe_render_dossier_pdf(
        merchant=merchant, docs=docs, ofac=ofac
    )

    audit.record(
        actor="dashboard",
        action="deal.submit_to_funders",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "funder_ids": [sub.funder_id for sub in files],
            "funder_names": [sub.funder_name for sub in files],
            "score_tier": score_result.tier,
            "score": score_result.score,
            "attachment_sha256": _sha256_hex(download_bytes),
            "attachment_filename": download_filename,
            "dossier_pdf_sha256": (
                _sha256_hex(dossier_pdf) if dossier_pdf is not None else None
            ),
            "dossier_pdf_filename": dossier_filename,
        },
    )

    # Update tracking fields (in-memory implementations only — Supabase
    # path round-trips lose these; durable record is the audit row above).
    try:
        merchants.upsert(
            merchant.model_copy(
                update={
                    "submitted_to_funder_ids": [
                        UUID(sub.funder_id) for sub in files
                    ],
                    "last_submitted_at": datetime.now(UTC),
                }
            )
        )
    except Exception as exc:
        # Tracking is best-effort; the audit row above is authoritative.
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "submission.tracking_update_failed merchant_id=%s err=%s",
            merchant.id,
            exc,
        )

    _record_submission_to_zoho(
        merchant=merchant,
        files=files,
        zip_bytes=download_bytes,
        zip_filename=download_filename,
        merchants=merchants,
        audit=audit,
        dossier_pdf=dossier_pdf,
        dossier_filename=dossier_filename,
    )

    return Response(
        content=download_bytes,
        media_type=download_media,
        headers={
            "content-disposition": f'attachment; filename="{download_filename}"'
        },
    )


_FUNDER_RESPONSE_STATUSES = frozenset({"approved", "declined", "countered", "pending"})


@router.post("/merchants/{merchant_id}/funder-response", response_model=None)
async def merchant_funder_response(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    funder_id: Annotated[str, Form()],
    response_status: Annotated[str, Form()],
    offered_amount: Annotated[str, Form()] = "",
    offered_factor: Annotated[str, Form()] = "",
    offered_term_days: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> Response:
    """Record one funder's reply to an AEGIS submission.

    v1 persistence is via ``audit_log`` only — the durable submissions
    table is Phase 7C. Reads pull the latest row per funder back through
    ``audit.list_for_subject(action='deal.funder_response')``, so the
    merchant-match panel always shows what the operator last typed.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    rs = response_status.strip().lower()
    if rs not in _FUNDER_RESPONSE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"response_status must be one of {sorted(_FUNDER_RESPONSE_STATUSES)}",
        )

    try:
        funder_uuid = UUID(funder_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid funder_id: {funder_id!r}",
        ) from exc

    try:
        funder = funder_repo.get(funder_uuid)
    except FunderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    try:
        amount = _decimal_or_none(offered_amount)
        factor = _decimal_or_none(offered_factor)
        term = _int_or_none(offered_term_days)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    audit.record(
        actor="dashboard",
        action="deal.funder_response",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "funder_id": str(funder_uuid),
            "funder_name": funder.name,
            "status": rs,
            "offered_amount": str(amount) if amount is not None else None,
            "offered_factor": str(factor) if factor is not None else None,
            "offered_term_days": term,
            "notes": notes.strip() or None,
        },
    )

    # Redirect back to the match panel so the operator sees the row land.
    return RedirectResponse(
        url=f"/ui/merchants/{merchant.id}/match", status_code=303
    )


def _latest_funder_responses(
    audit: AuditLog, merchant_id: UUID
) -> dict[str, dict[str, Any]]:
    """Pull latest ``deal.funder_response`` audit row per funder_id.

    Keyed by funder_id (string UUID). Empty dict if none recorded yet.
    Used by the match panel to render a status chip per submitted lender.
    """
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant_id,
        action="deal.funder_response",
        limit=200,
    )
    out: dict[str, dict[str, Any]] = {}
    # rows are newest-first, so the first row we see per funder_id wins.
    for r in rows:
        details = r.get("details") or {}
        fid = details.get("funder_id")
        if not isinstance(fid, str) or fid in out:
            continue
        out[fid] = {
            "status": details.get("status"),
            "offered_amount": details.get("offered_amount"),
            "offered_factor": details.get("offered_factor"),
            "offered_term_days": details.get("offered_term_days"),
            "notes": details.get("notes"),
            "recorded_at": r.get("created_at"),
        }
    return out


def _sha256_hex(payload: bytes) -> str:
    """Cheap content-addressable handle for an audit-log attachment row."""
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _maybe_render_dossier_pdf(
    *,
    merchant: MerchantRow,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> tuple[bytes | None, str | None]:
    """Render the merchant's PDF dossier for Zoho attachment, or fail soft.

    Returns ``(pdf_bytes, filename)`` on success; ``(None, None)`` if the
    Hetzner box / WSL2 native libs are unavailable. The submission flow
    must not fail just because a PDF can't be produced — the CSV ZIP
    download and the audit row are the authoritative record.
    """
    try:
        import weasyprint

        context = _build_pdf_dossier_context(merchant, docs, ofac)
        html = templates.get_template("merchant_detail_dossier_pdf.html.j2").render(
            context
        )
        pdf_bytes = cast(bytes, weasyprint.HTML(string=html).write_pdf())
        filename = f"{slugify(merchant.business_name)}_dossier.pdf"
        return pdf_bytes, filename
    except (OSError, ImportError) as exc:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "dossier_pdf_render_failed merchant_id=%s err=%s",
            merchant.id,
            exc,
        )
        return None, None


def _record_submission_to_zoho(
    *,
    merchant: MerchantRow,
    # list[FunderSubmissionFile] — looser annotation keeps helper import-light
    files: list[Any],
    zip_bytes: bytes,
    zip_filename: str,
    merchants: MerchantRepository,
    audit: AuditLog,
    dossier_pdf: bytes | None = None,
    dossier_filename: str | None = None,
) -> None:
    """Mirror the funder submission into Zoho (Deal + each Lender record).

    Best-effort: failures are audited but never raised — the CSV/ZIP
    download must complete even if Zoho is down. Skips silently when
    Zoho env isn't configured (e.g. tests, in-memory mode).
    """
    from aegis.logger import get_logger

    log = get_logger(__name__)

    if not merchant.zoho_deal_id:
        audit.record(
            actor="dashboard",
            action="zoho.submission.skipped_no_deal",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "funder_names": [sub.funder_name for sub in files],
                "reason": "merchant has no zoho_deal_id; push to Zoho first",
            },
        )
        return

    try:
        from aegis.zoho.client import ZohoClient
        from aegis.zoho.sync import ZohoSync

        zoho_client = ZohoClient()
        zoho_sync = ZohoSync(client=zoho_client, merchants=merchants, audit=audit)
    except Exception as exc:
        log.warning(
            "zoho client init failed during submission; skipping CRM update",
            extra={"merchant_id": str(merchant.id), "error": str(exc)},
        )
        audit.record(
            actor="dashboard",
            action="zoho.submission.skipped_unconfigured",
            subject_type="merchant",
            subject_id=merchant.id,
            details={"error": str(exc)},
        )
        return

    try:
        zoho_sync.record_funder_submission(
            merchant_id=merchant.id,
            funder_names=[sub.funder_name for sub in files],
            zip_bytes=zip_bytes,
            zip_filename=zip_filename,
            dossier_pdf=dossier_pdf,
            dossier_filename=dossier_filename,
        )
    except Exception as exc:
        log.warning(
            "zoho submission record failed",
            extra={"merchant_id": str(merchant.id), "error": str(exc)},
        )
        audit.record(
            actor="dashboard",
            action="zoho.submission.record_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={"error": str(exc)},
        )


def _parse_funder_ids(values: list[str]) -> list[UUID]:
    """Coerce form-encoded funder_id values into a deduped list of UUIDs."""
    out: list[UUID] = []
    seen: set[UUID] = set()
    for v in values:
        s = v.strip()
        if not s:
            continue
        try:
            u = UUID(s)
        except ValueError:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


@router.get("/merchants/{merchant_id}", response_class=HTMLResponse)
async def merchant_detail(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # Flash from /ui/intake — broker just created the merchant. Banner
    # rendered by the template when from_intake=1 is present in the URL.
    from_intake = request.query_params.get("from_intake") == "1"
    try:
        intake_docs_uploaded = int(request.query_params.get("docs") or "0")
    except ValueError:
        intake_docs_uploaded = 0
    try:
        intake_docs_failed = int(request.query_params.get("failed") or "0")
    except ValueError:
        intake_docs_failed = 0

    all_docs = docs.list_documents(merchant_id=merchant_id, limit=50)
    # Batch fetch analyses for every document in one query rather than
    # N+1 per-document calls. analyses_by_doc.get(doc.id) yields the
    # AnalysisRow when present, None when the document hasn't been
    # parsed yet.
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in all_docs])
    documents_table: list[dict[str, Any]] = [
        {"document": d, "analysis": analyses_by_doc.get(d.id)} for d in all_docs
    ]

    latest_doc = all_docs[0] if all_docs else None
    latest_analysis = analyses_by_doc.get(latest_doc.id) if latest_doc else None

    # Bundle switcher: ?bundle=<bank>|<last4>. Empty segments mean
    # "unknown" (encoded back as None). Falls back to most-populated
    # bundle when the param is absent or names a bundle that no longer
    # exists for this merchant.
    selected_bundle = _parse_bundle_query(request.query_params.get("bundle"))

    score_result = None
    stacking = None
    score_window = None
    bundle_summaries: list[dict[str, Any]] = []
    statement_coverage: dict[str, Any] | None = None
    pattern_cards: list[Any] = []
    soft_signals = (
        parse_soft_signal_flags(list(latest_doc.all_flags))
        if latest_doc is not None
        else None
    )
    if latest_doc is not None and latest_analysis is not None:
        all_items = _collect_analyzed_for_merchant(
            docs, merchant_id, window=999, bundle=None
        )
        bundle_options = _bundle_keys_for_merchant(all_items)
        if selected_bundle is not None and selected_bundle not in {
            k for k, _ in bundle_options
        }:
            selected_bundle = None
        items = _collect_analyzed_for_merchant(
            docs, merchant_id, bundle=selected_bundle
        )
        active_bundle = (
            selected_bundle
            if selected_bundle is not None
            else _select_default_bundle(all_items)
        )
        bundle_summaries = _build_bundle_summaries(bundle_options, active_bundle)
        # Pattern cards are re-derived on view rather than persisted: the
        # parser emits Pattern dataclasses whose fields (severity, detail,
        # source_ids) aren't on AnalysisRow yet, so the dashboard recomputes
        # from the stored transactions. ~100ms overhead for typical
        # statements — cheap relative to the value of source-row drill-down.
        # Phase 9: pattern_analysis is also feed into score_input so the
        # counterparty / detector signals reach the scorer.
        latest_transactions = docs.list_transactions(latest_doc.id)
        stacking = build_stacking_card(latest_analysis, latest_transactions)
        try:
            pattern_analysis = analyze_patterns(
                latest_transactions,
                latest_analysis.statement_period_start,
                latest_analysis.statement_period_end,
            )
        except Exception:
            pattern_analysis = None
        pattern_cards = list(
            build_pattern_cards(pattern_analysis, latest_transactions)
        )
        if items:
            score_input = _score_input_multi_month(
                merchant, items, pattern_analysis=pattern_analysis
            )
            try:
                score_result = score_deal(score_input, ofac=ofac)
            except OFACStaleError:
                score_result = None
            score_window = {
                "months_used": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "any_manual_review": any(
                    d.parse_status == "manual_review" for d, _ in items
                ),
            }
            statement_coverage = {
                "bundle_bank_name": active_bundle[0] if active_bundle else None,
                "bundle_account_last4": active_bundle[1] if active_bundle else None,
                "statements_in_bundle": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "missing_months": _detect_missing_months(items),
                "bundle_options": bundle_summaries,
            }

    state_tier = _state_tier(merchant.state)
    ofac_status, ofac_match = _ofac_ribbon_status(ofac, merchant.business_name)

    from aegis.api.routes.findings import _compute_trend

    trend = _compute_trend(all_docs, docs)
    history = audit.list_for_subject(
        subject_type="merchant", subject_id=merchant_id, limit=20
    )

    # Dossier is the only merchant-detail surface. The legacy v2 panel
    # template was retired when the whole app was unified on the dossier
    # aesthetic; ?view=v2 is accepted but ignored (kept reachable so any
    # bookmarked link still 200s instead of 404ing).
    template_name = "merchant_detail_dossier.html.j2"

    # Reshape state_tier into the richer dict the dossier template
    # expects. v2 template ignores extra keys. Citation / verified are
    # sourced from the STATES registry when present.
    state_reg = STATES.get(merchant.state.upper()) if merchant.state else None
    state_tier_dossier: dict[str, Any] | None = None
    if isinstance(state_tier, int):
        tier_summaries = {
            1: "Commercial-finance disclosure law applies. Pre-signature disclosure required.",
            2: "General state law applies. No MCA-specific statute; standard contract law governs.",
            3: "Served but not yet audited. Disclosure renderer raises StateNotAudited.",
        }
        state_tier_dossier = {
            "label": f"Tier {['', 'I', 'II', 'III'][state_tier]}",
            "summary": tier_summaries.get(state_tier, ""),
            "citation": getattr(state_reg, "citation_url", None)
            or getattr(state_reg, "statute_citation", None),
            "verified": getattr(state_reg, "verified_date", None),
        }

    # Map _ofac_ribbon_status output into the dossier's status keys.
    if ofac_status == "checked":
        ofac_dossier_status = "match" if ofac_match else "clean"
    elif ofac_status == "stale":
        ofac_dossier_status = "unavailable"
    elif ofac_status == "unavailable":
        ofac_dossier_status = "unavailable"
    else:
        ofac_dossier_status = "pending"

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "merchant": merchant,
            "documents": documents_table,
            "document": latest_doc,
            "analysis": latest_analysis,
            "aggregate_labels": _AGGREGATE_LABELS,
            "aggregate_unit_kind": _AGGREGATE_UNIT_KIND,
            "pattern_cards": pattern_cards,
            "soft_signals": soft_signals,
            "from_intake": from_intake,
            "intake_docs_uploaded": intake_docs_uploaded,
            "intake_docs_failed": intake_docs_failed,
            "score_result": score_result,
            "score_window": score_window,
            "statement_coverage": statement_coverage,
            "stacking": stacking,
            "state_tier": state_tier_dossier,
            "ofac_status": ofac_dossier_status,
            "ofac_match": ofac_match,
            "trend": trend,
            "history": history,
        },
    )


@router.get("/merchants/{merchant_id}/dossier.pdf")
async def merchant_dossier_pdf(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> Response:
    """Downloadable PDF dossier of the merchant.

    Same data as the HTML dossier at ``/ui/merchants/{id}`` but laid out
    for paper: US Letter, page-break controls, no HTMX, no sidebar,
    system fonts. Always renders the *default* bundle (most-populated
    bank/last4 pair); operators switching bundles on the dashboard get
    the on-screen view, not a separate PDF per bundle.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    context = _build_pdf_dossier_context(merchant, docs, ofac)
    template = templates.get_template("merchant_detail_dossier_pdf.html.j2")
    html = template.render(context)

    # WeasyPrint native libs (Pango / Cairo / HarfBuzz) ship on the
    # Hetzner production box via deploy/install.sh. Local Windows dev
    # boxes don't have them — both the import itself and the render
    # can OSError when libgobject / libpango aren't on the loader path.
    # Operators developing on Windows should use WSL2 (documented in
    # README) and see a useful 503 here instead of a 500 stack trace.
    try:
        import weasyprint

        pdf_bytes = cast(bytes, weasyprint.HTML(string=html).write_pdf())
    except (OSError, ImportError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"weasyprint native libs unavailable: {exc}. Run from "
                "WSL2 / Linux, or use the Hetzner production deploy."
            ),
        ) from exc

    filename = f"{slugify(merchant.business_name)}_dossier.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _build_pdf_dossier_context(
    merchant: MerchantRow,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> dict[str, Any]:
    """Build the print-template context (subset of merchant_detail's context).

    The print template only needs the fields it actually renders. This
    helper keeps the PDF route concise and the data sourcing identical
    to the HTML dossier (same scoring, same bundle pick, same pattern
    cards, same OFAC ribbon).
    """
    all_docs = docs.list_documents(merchant_id=merchant.id, limit=50)
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in all_docs])
    documents_table: list[dict[str, Any]] = [
        {"document": d, "analysis": analyses_by_doc.get(d.id)} for d in all_docs
    ]

    latest_doc = all_docs[0] if all_docs else None
    latest_analysis = analyses_by_doc.get(latest_doc.id) if latest_doc else None

    score_result = None
    score_window = None
    statement_coverage: dict[str, Any] | None = None
    stacking = None
    pattern_cards: list[Any] = []

    if latest_doc is not None and latest_analysis is not None:
        all_items = _collect_analyzed_for_merchant(
            docs, merchant.id, window=999, bundle=None
        )
        bundle_options = _bundle_keys_for_merchant(all_items)
        items = _collect_analyzed_for_merchant(docs, merchant.id, bundle=None)
        active_bundle = _select_default_bundle(all_items)

        # Phase 9: derive pattern_analysis BEFORE score_input so the
        # counterparty + Phase 9 detector signals reach the scorer.
        latest_transactions = docs.list_transactions(latest_doc.id)
        stacking = build_stacking_card(latest_analysis, latest_transactions)
        try:
            pattern_analysis = analyze_patterns(
                latest_transactions,
                latest_analysis.statement_period_start,
                latest_analysis.statement_period_end,
            )
        except Exception:
            pattern_analysis = None
        pattern_cards = list(build_pattern_cards(pattern_analysis, latest_transactions))

        if items:
            score_input = _score_input_multi_month(
                merchant, items, pattern_analysis=pattern_analysis
            )
            try:
                score_result = score_deal(score_input, ofac=ofac)
            except OFACStaleError:
                score_result = None
            score_window = {
                "months_used": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "any_manual_review": any(
                    d.parse_status == "manual_review" for d, _ in items
                ),
            }
            statement_coverage = {
                "bundle_bank_name": active_bundle[0] if active_bundle else None,
                "bundle_account_last4": active_bundle[1] if active_bundle else None,
                "statements_in_bundle": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "missing_months": _detect_missing_months(items),
                "bundle_options": _build_bundle_summaries(bundle_options, active_bundle),
            }

    state_tier = _state_tier(merchant.state)
    state_reg = STATES.get(merchant.state.upper()) if merchant.state else None
    state_tier_dossier: dict[str, Any] | None = None
    if isinstance(state_tier, int):
        tier_summaries = {
            1: "Commercial-finance disclosure law applies. Pre-signature disclosure required.",
            2: "General state law applies. No MCA-specific statute; standard contract law governs.",
            3: "Served but not yet audited. Disclosure renderer raises StateNotAudited.",
        }
        state_tier_dossier = {
            "label": f"Tier {['', 'I', 'II', 'III'][state_tier]}",
            "summary": tier_summaries.get(state_tier, ""),
            "citation": getattr(state_reg, "citation_url", None)
            or getattr(state_reg, "statute_citation", None),
            "verified": getattr(state_reg, "verified_date", None),
        }

    ofac_status_raw, ofac_match = _ofac_ribbon_status(ofac, merchant.business_name)
    if ofac_status_raw == "checked":
        ofac_dossier_status = "match" if ofac_match else "clean"
    elif ofac_status_raw == "stale":
        ofac_dossier_status = "unavailable"
    elif ofac_status_raw == "unavailable":
        ofac_dossier_status = "unavailable"
    else:
        ofac_dossier_status = "pending"

    return {
        "merchant": merchant,
        "document": latest_doc,
        "analysis": latest_analysis,
        "documents": documents_table,
        "score_result": score_result,
        "score_window": score_window,
        "statement_coverage": statement_coverage,
        "stacking": stacking,
        "pattern_cards": pattern_cards,
        "state_tier": state_tier_dossier,
        "ofac_status": ofac_dossier_status,
        "ofac_match": ofac_match,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


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
    filename = f"findings_{slugify(merchant.business_name)}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


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


# Hardcoded window: include the trailing 3 statement months in the
# multi-month score. Funder underwriting industry-norm is "last 3 bank
# statements" — more isn't more informative because business conditions
# change. Configurable later if a specific funder asks for 4 or 6.
_SCORE_WINDOW_MONTHS: int = 3


BundleKey = tuple[str | None, str | None]


def _bundle_key(analysis: AnalysisRow) -> BundleKey:
    """The (bank_name, account_last4) key used to group statements into bundles."""
    return (analysis.bank_name, analysis.account_last4)


def _bundle_keys_for_merchant(
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> list[tuple[BundleKey, int]]:
    """Return distinct bundle keys with their statement counts, most-populated first.

    Ties broken by latest ``statement_period_end`` so the most-recent
    bundle wins when two accounts have equal statement counts.
    """
    counts: dict[BundleKey, int] = {}
    latest_end: dict[BundleKey, date] = {}
    for _, analysis in items:
        key = _bundle_key(analysis)
        counts[key] = counts.get(key, 0) + 1
        if (
            key not in latest_end
            or analysis.statement_period_end > latest_end[key]
        ):
            latest_end[key] = analysis.statement_period_end

    def _sort_key(item: tuple[BundleKey, int]) -> tuple[int, date]:
        key, count = item
        return (count, latest_end[key])

    return sorted(counts.items(), key=_sort_key, reverse=True)


def _select_default_bundle(
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> BundleKey | None:
    """Pick the most-populated bundle, or ``None`` if no items.

    See ``_bundle_keys_for_merchant`` for the tiebreak rule.
    """
    keys = _bundle_keys_for_merchant(items)
    return keys[0][0] if keys else None


def _filter_to_bundle(
    items: list[tuple[DocumentRow, AnalysisRow]],
    bundle: BundleKey,
) -> list[tuple[DocumentRow, AnalysisRow]]:
    """Keep only items whose ``(bank_name, account_last4)`` matches ``bundle``."""
    return [(d, a) for d, a in items if _bundle_key(a) == bundle]


def _bundle_to_query(bundle: BundleKey) -> str:
    """Encode a bundle key as the ``?bundle=`` query value (``bank|last4``)."""
    bank, last4 = bundle
    return f"{bank or ''}|{last4 or ''}"


def _parse_bundle_query(value: str | None) -> BundleKey | None:
    """Parse a ``?bundle=`` query value back into a bundle key.

    Returns ``None`` for missing / blank / malformed inputs (caller then
    picks the default bundle). Empty segments are mapped to ``None`` so
    a pre-migration ``(None, None)`` bundle is addressable as ``|``.
    """
    if not value:
        return None
    parts = value.split("|", 1)
    if len(parts) != 2:
        return None
    bank, last4 = parts
    return (bank or None, last4 or None)


def _build_bundle_summaries(
    bundle_options: list[tuple[BundleKey, int]],
    active: BundleKey | None,
) -> list[dict[str, Any]]:
    """Template-friendly view of the merchant's bundles.

    Each entry carries the bank/last4 labels, the statement count, the
    URL-safe query value, and whether the bundle is currently active.
    """
    out: list[dict[str, Any]] = []
    for key, count in bundle_options:
        bank, last4 = key
        out.append(
            {
                "bank_name": bank,
                "account_last4": last4,
                "count": count,
                "query": _bundle_to_query(key),
                "is_active": key == active,
            }
        )
    return out


def _collect_analyzed_for_merchant(
    docs: DocumentRepository,
    merchant_id: UUID,
    *,
    window: int = _SCORE_WINDOW_MONTHS,
    bundle: BundleKey | None = None,
) -> list[tuple[DocumentRow, AnalysisRow]]:
    """Return up to ``window`` most-recent analyzed docs for a merchant.

    "Analyzed" means the document has an analysis row — i.e. extraction +
    validation + classification + aggregation all completed. ``manual_review``
    status is OK if the analysis exists (classification-confidence floor
    breaches still produce a usable analysis; the operator's decision is
    informed by including them).

    Returned newest first so the caller can pick the latest doc as the
    "current state" anchor and use the remainder as historical context.

    Bundling
    --------
    A merchant with two bank accounts produces two bundles of statements.
    Scoring across mixed-account statements is wrong: revenue sums across
    accounts double-count cash that just moved between them. The default
    behavior here is therefore "pick the most-populated bundle" — pass
    ``bundle`` explicitly to override (operator switching bundles in the
    UI). Pre-migration analyses without ``bank_name``/``account_last4``
    all share the ``(None, None)`` bundle and behave identically to the
    pre-bundling implementation.
    """
    rows = docs.list_documents(merchant_id=merchant_id, limit=window * 4)
    analyzed: list[tuple[DocumentRow, AnalysisRow]] = []
    for d in rows:
        a = docs.get_analysis(d.id)
        if a is None:
            continue
        analyzed.append((d, a))

    if not analyzed:
        return []

    selected_bundle = bundle if bundle is not None else _select_default_bundle(analyzed)
    if selected_bundle is None:
        return []

    filtered = _filter_to_bundle(analyzed, selected_bundle)
    return filtered[:window]


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




def _criteria_comparison(
    funder: FunderRow, score_input: ScoreInput
) -> list[dict[str, Any]]:
    """Side-by-side ``funder gate -> deal value`` rows for the merchant_match card.

    Only emits a row when the funder has set the gate (None means "no
    policy"). Status is "fail" / "warn" / "pass" so the template can
    color the row without re-deriving the matcher rule.
    """
    rows: list[dict[str, Any]] = []

    # The comparison values are stringified or filtered at render time;
    # widening to ``object`` keeps mypy strict happy without forcing the
    # caller to pre-coerce numeric / string / None values.
    def add(
        label: str,
        funder_value: object,
        deal_value: object,
        passed: bool,
        *,
        unit: str = "",
        soft: bool = False,
    ) -> None:
        if soft and not passed:
            status_str = "warn"
        elif passed:
            status_str = "pass"
        else:
            status_str = "fail"
        rows.append(
            {
                "label": label,
                "funder_value": funder_value,
                "deal_value": deal_value,
                "status": status_str,
                "unit": unit,
            }
        )

    if funder.min_monthly_revenue is not None:
        add(
            "Minimum monthly revenue",
            funder.min_monthly_revenue,
            score_input.monthly_revenue,
            score_input.monthly_revenue >= funder.min_monthly_revenue,
            unit="money",
        )
    if funder.min_avg_daily_balance is not None:
        add(
            "Minimum average daily balance",
            funder.min_avg_daily_balance,
            score_input.avg_daily_balance,
            score_input.avg_daily_balance >= funder.min_avg_daily_balance,
            unit="money",
        )
    if funder.min_credit_score is not None:
        if score_input.credit_score is None:
            add(
                "Minimum credit score",
                funder.min_credit_score,
                "missing",
                False,
                unit="fico",
                soft=True,
            )
        else:
            add(
                "Minimum credit score",
                funder.min_credit_score,
                score_input.credit_score,
                score_input.credit_score >= funder.min_credit_score,
                unit="fico",
            )
    if funder.min_months_in_business is not None:
        if score_input.time_in_business_months is None:
            add(
                "Minimum time in business",
                funder.min_months_in_business,
                "missing",
                False,
                unit="months",
                soft=True,
            )
        else:
            add(
                "Minimum time in business",
                funder.min_months_in_business,
                score_input.time_in_business_months,
                score_input.time_in_business_months >= funder.min_months_in_business,
                unit="months",
            )
    if funder.max_positions is not None:
        add(
            "Maximum stacked positions",
            funder.max_positions,
            score_input.mca_positions,
            score_input.mca_positions <= funder.max_positions,
            unit="count",
        )
    if not funder.accepts_stacking and score_input.mca_positions > 0:
        add(
            "Stacking acceptance",
            "does not stack",
            f"{score_input.mca_positions} existing position(s)",
            False,
            unit="",
        )
    if funder.max_nsf_tolerance is not None:
        add(
            "Maximum NSF count",
            funder.max_nsf_tolerance,
            score_input.num_nsf,
            score_input.num_nsf <= funder.max_nsf_tolerance,
            unit="count",
        )
    if funder.max_advance is not None:
        add(
            "Maximum advance",
            funder.max_advance,
            score_input.requested_amount,
            score_input.requested_amount <= funder.max_advance,
            unit="money",
        )
    if funder.min_advance is not None:
        add(
            "Minimum advance",
            funder.min_advance,
            score_input.requested_amount,
            score_input.requested_amount >= funder.min_advance,
            unit="money",
        )
    if score_input.industry_naics and funder.excluded_industries:
        excluded = any(
            score_input.industry_naics.startswith(x) for x in funder.excluded_industries
        )
        add(
            "Industry exclusion",
            ", ".join(funder.excluded_industries[:5])
            + ("..." if len(funder.excluded_industries) > 5 else ""),
            score_input.industry_naics,
            not excluded,
            unit="",
        )
    if funder.excluded_states:
        excluded_st = score_input.state in funder.excluded_states
        add(
            "State exclusion",
            ", ".join(funder.excluded_states[:8])
            + ("..." if len(funder.excluded_states) > 8 else ""),
            score_input.state,
            not excluded_st,
            unit="",
        )
    return rows


def _state_compliance_card(state: str) -> dict[str, Any] | None:
    """Pull CoJ + broker-fee citations from compliance.states for the card."""
    try:
        reg = STATES.get(state)
    except (AttributeError, KeyError):
        return None
    if reg is None:
        return None
    out: dict[str, Any] = {"state": state, "tier": getattr(reg, "tier", None)}
    coj = getattr(reg, "coj_allowed", None)
    out["coj_allowed"] = coj
    out["coj_citation"] = getattr(reg, "coj_citation", None) or getattr(
        reg, "citation_url", None
    )
    out["broker_fees_prohibited"] = getattr(
        reg, "broker_advance_fees_prohibited", False
    )
    out["broker_fees_citation"] = getattr(reg, "broker_fees_citation", None) or getattr(
        reg, "statute_citation", None
    )
    return out


def _match_card(
    funder: FunderRow,
    match: FunderMatch,
    score_input: ScoreInput | None = None,
) -> dict[str, Any]:
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

    When ``score_input`` is supplied, the card also carries a side-by-side
    criteria comparison and the state regulatory context — both rendered
    as an expandable details block inside the funder card.
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

    criteria: list[dict[str, Any]] = []
    state_ctx: dict[str, Any] | None = None
    if score_input is not None:
        criteria = _criteria_comparison(funder, score_input)
        state_ctx = _state_compliance_card(score_input.state)

    return {
        "funder_id": str(funder.id),
        "funder_name": funder.name,
        "match_score": match.match_score,
        "color": color,
        "hard_reasons": hard_reasons,
        "soft_concerns": soft_concerns,
        "criteria_comparison": criteria,
        "state_compliance": state_ctx,
        "funder_requires_coj": funder.requires_coj,
        "funder_charges_merchant_advance_fees": funder.charges_merchant_advance_fees,
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


# ---------------------------------------------------------------------------
# Phase 10 — operator override capture (mp §20).
# ---------------------------------------------------------------------------
#
# Operator clicks "I disagree" on the dossier, picks a reason code +
# (optionally) typed-in pattern false-positives, and AEGIS persists an
# ``overrides`` row tied to ``decision_id``. Outcome stamping lives on
# the funder_replies side (refinement 5); ``record_override`` back-
# stamps from any pending reply at creation time.
#
# The /ui surface is gated by Cloudflare Access in production (not
# require_bearer), matching the rest of this router.


@router.post(
    "/decisions/{decision_id}/override",
    response_model=None,
    include_in_schema=False,
)
async def decision_override(
    decision_id: UUID,
    audit: Annotated[AuditLog, Depends(get_audit)],
    override_repo: Annotated[OverrideRepository, Depends(get_override_repository)],
    reply_repo: Annotated[FunderReplyRepository, Depends(get_funder_reply_repository)],
    deal_id: Annotated[UUID, Form()],
    original_recommendation: Annotated[str, Form()],
    operator_decision: Annotated[str, Form()],
    reason_code: Annotated[str, Form()],
    reason_detail: Annotated[str, Form()] = "",
    pattern_false_positive: Annotated[str, Form()] = "",
) -> JSONResponse:
    """Persist one operator override + back-stamp from pending replies.

    ``pattern_false_positive`` is a comma-separated list of detector
    codes (the modal renders the active detectors as checkboxes and
    serializes the selection into one form field). Empty entries are
    dropped so a blank submit doesn't write ``[""]`` to the array
    column.
    """
    patterns = [p.strip() for p in pattern_false_positive.split(",") if p.strip()]
    try:
        payload = OverridePayload(
            deal_id=deal_id,
            decision_id=decision_id,
            original_recommendation=original_recommendation,
            operator_decision=operator_decision,
            reason_code=reason_code,
            reason_detail=reason_detail.strip() or None,
            pattern_false_positive=patterns,
            operator_id="dashboard",
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid override payload: {exc}",
        ) from exc

    try:
        result = record_override(
            payload,
            repo=override_repo,
            reply_repo=reply_repo,
            audit=audit,
        )
    except OverrideError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"override_persist_unavailable: {exc}",
        ) from exc

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "override_id": str(result.override_id),
            "back_stamped_outcome": result.back_stamped_outcome,
        },
    )


__all__ = ["router"]
