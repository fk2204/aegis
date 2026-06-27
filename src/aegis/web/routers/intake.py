"""Intake sub-router — combined create-merchant + upload-statements form.

Routes:
  * ``GET /ui/intake``   — render the intake form
  * ``POST /ui/intake``  — create merchant + (optional) attach N PDFs, then
                           redirect to the merchant detail page

Behavior on ``POST``:
  * State must be served (``_validate_merchant_state``); unserved → 400
    re-render preserving the entered form values.
  * Files are optional — operator may create the merchant first and upload
    later from ``/ui/upload``.
  * On any uploader error the merchant IS still created; the failure is
    recorded as ``intake.partial_failure`` in ``audit_log`` and the
    redirect surfaces the failed count via query params.

Extracted from ``router.py`` during R4.1. Shares the upload helper +
form validators with ``upload.py`` and the still-resident merchants
routes via ``aegis.web._router_helpers``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse

from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.background_checks import enqueue_background_checks
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantConflictError,
    MerchantRepository,
)
from aegis.ops.operators import resolve_operator_email
from aegis.storage import DocumentRepository
from aegis.web._router_helpers import (
    _entity_type_or_none,
    _persist_uploads,
    _validate_merchant_state,
)
from aegis.web._templates import templates

router = APIRouter()


def _intake_form_error(request: Request, error: str, form: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "intake.html.j2",
        {"error": error, "form": form},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@router.get("/intake", response_class=HTMLResponse)
async def intake_form(request: Request) -> HTMLResponse:
    """Combined intake: create merchant + upload N statements in one POST."""
    return templates.TemplateResponse(request, "intake.html.j2", {"error": None, "form": {}})


@router.post("/intake", response_class=HTMLResponse, response_model=None)
async def intake_submit(
    request: Request,
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    business_name: Annotated[str, Form()],
    owner_name: Annotated[str, Form()],
    state: Annotated[str, Form()],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
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

    # Fire-and-forget background-checks sweep (UCC + web-presence).
    # Same posture as the webhook + standalone create paths — enqueue
    # failures are absorbed inside the helper; the dossier Refresh
    # buttons remain the manual fallback.
    await enqueue_background_checks(
        request=request,
        merchant_id=merchant.id,
        audit=audit,
        trigger="ui_intake",
    )

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
            actor_email=actor_email,
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
                actor_email=actor_email,
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
