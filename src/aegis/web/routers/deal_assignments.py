"""Deal-assignment routes (commit 2 of the role/assignments/notifications wave).

Three HTMX surfaces:

* ``GET  /ui/merchants/{merchant_id}/assignment-modal`` — HTMX fragment
  rendering the operator-picker modal (lists every active operator with
  a button that POSTs the assign).
* ``POST /ui/merchants/{merchant_id}/assign`` — assign an operator to a
  merchant. Form body: ``operator_id``. Returns the refreshed chip.
* ``POST /ui/merchants/{merchant_id}/unassign`` — remove the existing
  assignment. Returns the refreshed (empty) chip.

Audit-log: every assign/unassign writes a ``merchant.assignment.created``
or ``merchant.assignment.removed`` row. Permission gate: assign / unassign
require Admin OR Underwriter; the modal is readable by any role.

The chip + modal are rendered via ``_assignment_chip.html.j2`` and
``_assignment_modal.html.j2`` so the same fragments power the dossier
header and any future surfaces that need the chip.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_deal_assignment_repository,
    get_merchant_repository,
    get_operator_repository,
)
from aegis.audit import AuditLog
from aegis.merchants.repository import (
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.ops.deal_assignment_repository import (
    DealAssignmentRepository,
)
from aegis.ops.operator_repository import OperatorRepository
from aegis.ops.operators import Operator
from aegis.web._role_gate import underwriter_or_admin
from aegis.web._templates import templates

router = APIRouter()


def _render_chip(
    request: Request,
    *,
    merchant_id: UUID,
    operators: OperatorRepository,
    assignments: DealAssignmentRepository,
) -> HTMLResponse:
    """Render the assignment chip (assigned operator or 'Unassigned')."""
    assignment = assignments.get_for_merchant(merchant_id)
    assignee: Operator | None = None
    if assignment is not None:
        assignee = operators.get_by_id(assignment.operator_id)
    return templates.TemplateResponse(
        request,
        "_assignment_chip.html.j2",
        {
            "merchant_id": merchant_id,
            "assignee": assignee,
        },
    )


@router.get(
    "/merchants/{merchant_id}/assignment-modal",
    response_class=HTMLResponse,
)
async def assignment_modal(
    request: Request,
    merchant_id: UUID,
    operators: Annotated[OperatorRepository, Depends(get_operator_repository)],
    assignments: Annotated[DealAssignmentRepository, Depends(get_deal_assignment_repository)],
) -> HTMLResponse:
    """HTMX fragment: render the operator-picker modal.

    Lists every active operator. Buttons POST to /assign with the chosen
    operator_id. The currently-assigned operator is marked so the
    operator can't accidentally re-assign to the same person.
    """
    current = assignments.get_for_merchant(merchant_id)
    return templates.TemplateResponse(
        request,
        "_assignment_modal.html.j2",
        {
            "merchant_id": merchant_id,
            "current_operator_id": current.operator_id if current else None,
            "operators": operators.list_active(),
        },
    )


@router.post(
    "/merchants/{merchant_id}/assign",
    response_class=HTMLResponse,
)
async def assign_merchant(
    request: Request,
    merchant_id: UUID,
    operator_id: Annotated[UUID, Form(...)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    operators: Annotated[OperatorRepository, Depends(get_operator_repository)],
    assignments: Annotated[DealAssignmentRepository, Depends(get_deal_assignment_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor: Annotated[Operator, Depends(underwriter_or_admin)],
) -> HTMLResponse:
    """Assign ``operator_id`` to ``merchant_id``. Replaces any existing
    assignment (the repository removes the prior row before inserting).
    """
    try:
        merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="merchant not found"
        ) from exc
    target = operators.get_by_id(operator_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="operator not found")

    previous = assignments.get_for_merchant(merchant_id)
    if previous is not None:
        audit.record(
            action="merchant.assignment.removed",
            actor=f"operator:{actor.email}",
            actor_email=actor.email,
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "previous_operator_id": str(previous.operator_id),
                "reason": "reassign",
            },
        )

    assignments.assign(
        merchant_id=merchant_id,
        operator_id=operator_id,
        assigned_by=actor.id,
    )
    audit.record(
        action="merchant.assignment.created",
        actor=f"operator:{actor.email}",
        actor_email=actor.email,
        subject_type="merchant",
        subject_id=merchant_id,
        details={"operator_id": str(operator_id)},
    )

    return _render_chip(
        request,
        merchant_id=merchant_id,
        operators=operators,
        assignments=assignments,
    )


@router.post(
    "/merchants/{merchant_id}/unassign",
    response_class=HTMLResponse,
)
async def unassign_merchant(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    operators: Annotated[OperatorRepository, Depends(get_operator_repository)],
    assignments: Annotated[DealAssignmentRepository, Depends(get_deal_assignment_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor: Annotated[Operator, Depends(underwriter_or_admin)],
) -> HTMLResponse:
    """Remove the existing assignment for ``merchant_id``."""
    try:
        merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="merchant not found"
        ) from exc

    removed = assignments.unassign(merchant_id)
    if removed is not None:
        audit.record(
            action="merchant.assignment.removed",
            actor=f"operator:{actor.email}",
            actor_email=actor.email,
            subject_type="merchant",
            subject_id=merchant_id,
            details={
                "previous_operator_id": str(removed.operator_id),
                "reason": "unassign",
            },
        )

    return _render_chip(
        request,
        merchant_id=merchant_id,
        operators=operators,
        assignments=assignments,
    )


__all__ = ["router"]
