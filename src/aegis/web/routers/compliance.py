"""Compliance sub-router — operator override + obligations dashboard.

Routes:
  * ``POST /ui/decisions/{decision_id}/override``       — legacy override
  * ``POST /ui/merchants/{merchant_id}/documents/{document_id}/override``
                                                        — dossier override flow
  * ``GET  /ui/overrides/summary``                      — confusion matrix
  * ``GET  /ui/compliance/obligations``                 — state registration deadlines
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
    get_repository,
)
from aegis.audit import AuditLog, AuditWriteError
from aegis.compliance.overrides import (
    OUTCOME_COLUMNS,
    DossierOverridePayload,
    OverrideError,
    OverridePayload,
    OverrideRepository,
    build_reason_code_summary,
    record_dossier_override,
    record_override,
)
from aegis.funders.replies import FunderReplyRepository
from aegis.ops.operators import resolve_operator_email
from aegis.storage import DocumentNotFoundError, DocumentRepository
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


@router.post(
    "/merchants/{merchant_id}/documents/{document_id}/override",
    response_model=None,
    include_in_schema=False,
)
async def dossier_override(
    request: Request,
    merchant_id: UUID,
    document_id: UUID,
    audit: Annotated[AuditLog, Depends(get_audit)],
    override_repo: Annotated[OverrideRepository, Depends(get_override_repository)],
    documents: Annotated[DocumentRepository, Depends(get_repository)],
    operator_email: Annotated[str | None, Depends(resolve_operator_email)],
    operator_decision: Annotated[str, Form()],
    reason_code: Annotated[str, Form()],
    original_recommendation: Annotated[str, Form()],
    reason_detail: Annotated[str, Form()] = "",
    decision_id: Annotated[str, Form()] = "",
    pattern_false_positives: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    """Phase 10 dossier "Override recommendation" submission.

    Form fields:
      * ``operator_decision``       — 'approve' or 'decline'
      * ``reason_code``             — one of the ReasonCode literal values
      * ``original_recommendation`` — current parse_status of the doc
                                       ('proceed', 'decline', 'manual_review')
      * ``reason_detail``           — freeform operator note (≤2000 chars)
      * ``decision_id``             — UUID string (optional; older docs
                                       have no decisions row)
      * ``pattern_false_positives`` — repeated form values (one per
                                       ticked checkbox in the modal)

    On success:
      * Inserts an overrides row (migration 072 schema).
      * Flips ``documents.parse_status`` per
        ``PARSE_STATUS_AFTER_OVERRIDE``.
      * Writes a ``deal.operator_override`` audit row.
      * Returns 303-redirect to the dossier so the browser GET reloads
        the page with the updated parse_status header.

    Auth model: the route reads
    ``cf-access-authenticated-user-email`` for the operator identity
    via ``resolve_operator_email``. The header is set by Cloudflare
    Access on every request that crosses the tunnel; local /
    test paths fall back to ``operator_id='dashboard'`` matching the
    legacy override route. A future RBAC tightening (per
    aegis.ops.operators.OperatorRole) would compose a second
    dependency that 401s when the header is missing in production.
    """
    # Parse / coerce form inputs into the strict payload shape.
    parsed_decision_id: UUID | None = None
    if decision_id.strip():
        try:
            parsed_decision_id = UUID(decision_id.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid decision_id: {decision_id!r}",
            ) from exc

    # Strip per-item whitespace + drop empties so a stray '' in the
    # form data doesn't write [""] to the array column.
    patterns_cleaned = [p.strip() for p in (pattern_false_positives or []) if p.strip()]

    operator_id = operator_email or "dashboard"
    try:
        payload = DossierOverridePayload(
            merchant_id=merchant_id,
            document_id=document_id,
            decision_id=parsed_decision_id,
            original_recommendation=original_recommendation,
            operator_decision=operator_decision,
            reason_code=reason_code,
            reason_detail=reason_detail.strip() or None,
            pattern_false_positives=patterns_cleaned,
            operator_id=operator_id,
            operator_email=operator_email,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid override payload: {exc}",
        ) from exc

    try:
        record_dossier_override(
            payload,
            override_repo=override_repo,
            documents=documents,
            audit=audit,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"document not found: {document_id}",
        ) from exc
    except OverrideError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"override_persist_unavailable: {exc}",
        ) from exc
    except AuditWriteError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"override_audit_unavailable: {exc}",
        ) from exc

    # HTMX: redirect via HX-Redirect so the modal closes and the
    # dossier reloads (showing the updated parse_status + new
    # override-history row). Non-HTMX form posts get a normal 303
    # so the browser does the same.
    dossier_url = f"/ui/merchants/{merchant_id}"
    if "hx-request" in {k.lower() for k in request.headers.keys()}:
        return HTMLResponse(
            content="",
            status_code=status.HTTP_200_OK,
            headers={"HX-Redirect": dossier_url},
        )
    return HTMLResponse(
        content="",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": dossier_url},
    )


@router.get("/overrides/summary", response_class=HTMLResponse)
async def overrides_summary(
    request: Request,
    override_repo: Annotated[OverrideRepository, Depends(get_override_repository)],
) -> HTMLResponse:
    """Confusion-matrix view of every operator override per reason_code.

    Master plan §20 task 4: "Quarterly report: confusion matrix per
    reason code (operator overrode AEGIS decline → outcome = funded
    vs declined-by-funder vs charged-off)."

    Columns: count + each value of ``OUTCOME_COLUMNS`` (funded,
    declined_by_funder, charged_off, paid_in_full, pending). Rows
    sorted by total descending. Renders an empty-state on day-zero
    (no overrides captured yet).

    NOTE on the funder_replies JOIN: the master plan calls for joining
    on funder_replies' outcome column. This view reads only
    ``overrides.outcome`` — populated by the funder-reply ingestion
    side (``aegis.funders.replies.stamp_override_from_replies``) and
    by a future operator-paste flow. Until the override-outcome
    stamping path is wired into the dossier flow, EVERY new override
    falls into the ``pending`` bucket — the operator's first read
    post-deploy is "what reason_codes are firing, all pending" which
    is the right read.
    """
    rows = override_repo.list_for_summary()
    summary = build_reason_code_summary(rows)
    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "overrides_summary.html.j2",
            {
                "active": "Compliance",
                "summary_rows": summary,
                "outcome_columns": list(OUTCOME_COLUMNS),
                "total_overrides": sum(r.total for r in summary),
            },
        ),
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
