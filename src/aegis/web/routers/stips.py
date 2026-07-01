"""Stips web router — list / add / update / delete stips per merchant.

Mounted at ``/ui/merchants/{merchant_id}/stips`` under the main UI
prefix. Consumes ``aegis.stips.StipRepository`` via FastAPI Depends
so tests can swap the in-memory backend.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_audit
from aegis.audit import AuditLog
from aegis.stips import (
    STIP_TEMPLATES,
    StipNotFoundError,
    StipRepository,
    SupabaseStipRepository,
)
from aegis.stips.models import StipStatus, StipType
from aegis.web._templates import templates

router = APIRouter()


def get_stip_repository() -> StipRepository:
    """Default dependency — the Supabase backend. Tests override this
    with ``InMemoryStipRepository`` via ``app.dependency_overrides``."""
    return SupabaseStipRepository()


@router.get(
    "/merchants/{merchant_id}/stips",
    response_model=None,
)
async def merchant_list_stips(
    request: Request,
    merchant_id: UUID,
    stips: Annotated[StipRepository, Depends(get_stip_repository)],
) -> HTMLResponse:
    """Return the stips-section HTML partial for the dossier."""
    rows = stips.list_for_merchant(merchant_id)
    outstanding_count = sum(1 for r in rows if r.status == "outstanding")
    response = templates.TemplateResponse(
        request,
        "_stips_section.html.j2",
        {
            "merchant_id": merchant_id,
            "stips": rows,
            "outstanding_stips_count": outstanding_count,
        },
    )
    return response


@router.get(
    "/merchants/{merchant_id}/stips/add-form",
    response_model=None,
)
async def merchant_stip_add_form(
    request: Request,
    merchant_id: UUID,
) -> HTMLResponse:
    """Return the "add stip" form partial. HTMX loads it into
    ``#stip-add-form`` on the dossier."""
    response = templates.TemplateResponse(
        request,
        "_stip_add_form.html.j2",
        {
            "merchant_id": merchant_id,
            "templates_list": STIP_TEMPLATES,
        },
    )
    return response


_VALID_STATUSES: tuple[StipStatus, ...] = (
    "outstanding",
    "received",
    "waived",
    "expired",
)
_VALID_TYPES: tuple[StipType, ...] = (
    "document",
    "verification",
    "condition",
    "signature",
)


@router.post(
    "/merchants/{merchant_id}/stips",
    response_model=None,
)
async def merchant_create_stip(
    request: Request,
    merchant_id: UUID,
    stips: Annotated[StipRepository, Depends(get_stip_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    stip_type: Annotated[str, Form()],
    description: Annotated[str, Form()],
    due_date_str: Annotated[str | None, Form(alias="due_date")] = None,
    notes: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Add a stipulation. Returns the refreshed stips table partial."""
    if stip_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"stip_type must be one of {list(_VALID_TYPES)}; got {stip_type!r}"),
        )
    description = description.strip()
    if not description:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="description required",
        )

    parsed_due: date | None = None
    if due_date_str and due_date_str.strip():
        try:
            parsed_due = date.fromisoformat(due_date_str.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"due_date must be YYYY-MM-DD: {exc}",
            ) from exc

    stip_type_typed: StipType = stip_type
    new_row = stips.create(
        merchant_id=merchant_id,
        stip_type=stip_type_typed,
        description=description,
        due_date=parsed_due,
        notes=notes,
    )
    audit.record(
        actor="ui:stips",
        action="stip.created",
        subject_type="stip",
        subject_id=new_row.id,
        details={
            "merchant_id": str(merchant_id),
            "stip_type": stip_type,
            "description": description[:120],
        },
    )
    return await merchant_list_stips(request, merchant_id, stips)


@router.patch(
    "/merchants/{merchant_id}/stips/{stip_id}",
    response_model=None,
)
async def merchant_update_stip(
    request: Request,
    merchant_id: UUID,
    stip_id: UUID,
    stips: Annotated[StipRepository, Depends(get_stip_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    new_status: Annotated[str, Form(alias="status")],
    waived_reason: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Flip a stip's status. Returns the refreshed table partial.

    Also notifies when the last outstanding stip is marked received:
    an audit row ``merchant.all_stips_received`` fires so the
    dashboard / dossier can surface "ready to fund." No structured
    ``notifications`` write yet (deliberate — the notifications
    table is operator-scoped and this cron has no operator id).
    """
    if new_status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"status must be one of {list(_VALID_STATUSES)}; got {new_status!r}"),
        )

    new_status_typed: StipStatus = new_status
    try:
        updated = stips.update_status(
            merchant_id=merchant_id,
            stip_id=stip_id,
            status=new_status_typed,
            waived_reason=waived_reason,
        )
    except StipNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"stip {stip_id} not found for merchant {merchant_id}",
        ) from exc

    audit.record(
        actor="ui:stips",
        action="stip.status_changed",
        subject_type="stip",
        subject_id=updated.id,
        details={
            "merchant_id": str(merchant_id),
            "new_status": new_status,
            "waived_reason": (waived_reason or "")[:120],
        },
    )

    # If this flip cleared the last outstanding stip, emit a
    # "ready to fund" audit row that the dashboard can surface.
    if new_status in ("received", "waived", "expired"):
        remaining = stips.count_outstanding(merchant_id)
        if remaining == 0:
            audit.record(
                actor="ui:stips",
                action="merchant.all_stips_received",
                subject_type="merchant",
                subject_id=merchant_id,
                details={
                    "trigger_stip_id": str(stip_id),
                    "message": "All stipulations satisfied — ready to fund.",
                },
            )

    return await merchant_list_stips(request, merchant_id, stips)


@router.delete(
    "/merchants/{merchant_id}/stips/{stip_id}",
    response_model=None,
)
async def merchant_delete_stip(
    request: Request,
    merchant_id: UUID,
    stip_id: UUID,
    stips: Annotated[StipRepository, Depends(get_stip_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Remove a stip. Returns the refreshed table partial."""
    try:
        stips.get(merchant_id, stip_id)
    except StipNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"stip {stip_id} not found for merchant {merchant_id}",
        ) from exc
    stips.delete(merchant_id, stip_id)
    audit.record(
        actor="ui:stips",
        action="stip.deleted",
        subject_type="stip",
        subject_id=stip_id,
        details={"merchant_id": str(merchant_id)},
    )
    return await merchant_list_stips(request, merchant_id, stips)


__all__ = ["get_stip_repository", "router"]


# Referenced from function-local type annotations only, which mypy
# reads at import time but Ruff sees as "unused". Bind here so the
# names remain reachable if we ever move validation to a shared helper.
_ = (_VALID_STATUSES, _VALID_TYPES, Any)
