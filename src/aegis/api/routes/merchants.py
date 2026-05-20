"""/merchants — list, create, fetch, update merchants.

Operator-facing CRUD. The ``state`` field is the routing key for
compliance (Tier 1/2/3 disclosure routing) so we validate it against the
served-state inventory at create-and-update time and reject upstream
with ``state_not_served``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit, get_merchant_repository
from aegis.audit import AuditLog
from aegis.compliance.states import StateNotServed, validate_state_served
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantConflictError,
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.ops.operators import resolve_operator_email

router = APIRouter(
    prefix="/merchants",
    tags=["merchants"],
    dependencies=[Depends(require_bearer)],
)


@router.get("", response_model=list[MerchantRow])
def list_merchants(
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    state: Annotated[
        str | None, Query(min_length=2, max_length=2, description="USPS state code")
    ] = None,
) -> list[MerchantRow]:
    return repo.list_all(state=state)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MerchantRow)
def create_merchant(
    merchant: MerchantRow,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> MerchantRow:
    _enforce_state_served(merchant.state)
    try:
        saved = repo.upsert(merchant)
    except MerchantConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit.record(
        actor="api",
        actor_email=actor_email,
        action="merchant.create",
        subject_type="merchant",
        subject_id=saved.id,
        details={"state": saved.state, "industry_naics": saved.industry_naics},
    )
    return saved


@router.get("/{merchant_id}", response_model=MerchantRow)
def get_merchant(
    merchant_id: UUID,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> MerchantRow:
    try:
        return repo.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/{merchant_id}", response_model=MerchantRow)
def update_merchant(
    merchant_id: UUID,
    merchant: MerchantRow,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> MerchantRow:
    if merchant.id != merchant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="path id and body id must match",
        )
    _enforce_state_served(merchant.state)
    try:
        saved = repo.upsert(merchant)
    except MerchantConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit.record(
        actor="api",
        actor_email=actor_email,
        action="merchant.update",
        subject_type="merchant",
        subject_id=saved.id,
        details={"state": saved.state, "industry_naics": saved.industry_naics},
    )
    return saved


def _enforce_state_served(state: str) -> None:
    try:
        validate_state_served(state)
    except StateNotServed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


__all__ = ["router"]
