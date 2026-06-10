"""Compliance sub-router — operator override + obligations dashboard.

Routes:
  * ``POST /ui/decisions/{decision_id}/override``  — record operator override
  * ``GET  /ui/compliance/obligations``             — state registration deadlines
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from aegis.api.deps import (
    get_audit,
    get_funder_reply_repository,
    get_override_repository,
)
from aegis.audit import AuditLog
from aegis.compliance.overrides import (
    OverrideError,
    OverridePayload,
    OverrideRepository,
    record_override,
)
from aegis.funders.replies import FunderReplyRepository
from aegis.web._templates import templates

router = APIRouter()


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


@router.get("/compliance/obligations", response_class=HTMLResponse)
async def compliance_obligations(request: Request) -> HTMLResponse:
    """Operator view of state registration / annual-report obligations.

    Reads from `compliance_obligations` (migration 018). Rows are annotated
    in-Python with a `derived_state` (overdue / due_soon / on_track) so
    the template stays date-math-free.
    """
    from aegis.compliance.obligations import (
        get_obligations_repository,
        summarize,
    )

    repo = get_obligations_repository()
    rows = repo.list_obligations()
    summary = summarize(rows)

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "compliance_obligations.html.j2",
            {
                "active": "Compliance",
                "obligations": rows,
                "summary": summary,
            },
        ),
    )
