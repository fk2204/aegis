"""Renewals sub-router — operator-visibility calendar + funder attestations.

Routes:
  * ``GET  /ui/renewals``                             — upcoming-maturity calendar
  * ``POST /ui/renewals/{merchant_id}/attest``        — record funder attestation

Per ``.claude/rules/compliance.md`` SCOPE NOTE: AEGIS does NOT own the
regulator-facing disclosure obligation. Rows written here record OPERATOR
CLAIMS about the funder's behavior; they are not regulator-facing audit
artifacts. Funder partners' own audit trails remain the regulator record.
"""

from __future__ import annotations

import urllib.parse
from datetime import date, datetime
from typing import Annotated, Final, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_renewal_attestation_repository,
)
from aegis.audit import AuditLog
from aegis.merchants.renewal_attestations import (
    RenewalAttestationConflictError,
    RenewalAttestationRepository,
    RenewalAttestationWriteError,
    record_renewal_attestation,
)
from aegis.merchants.repository import (
    MerchantNotFoundError,
    MerchantRepository,
    list_renewal_pipeline,
    list_upcoming_renewals,
)
from aegis.ops.operators import resolve_operator_email
from aegis.web._templates import templates

router = APIRouter()


_RENEWAL_WINDOW_DEFAULT_DAYS: Final[int] = 90
_RENEWAL_WINDOW_MAX_DAYS: Final[int] = 365


@router.get("/renewals", response_class=HTMLResponse)
async def upcoming_renewals(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    attestations_repo: Annotated[
        RenewalAttestationRepository,
        Depends(get_renewal_attestation_repository),
    ],
    window_days: Annotated[
        int,
        Query(
            ge=1,
            le=_RENEWAL_WINDOW_MAX_DAYS,
            description="Lookahead window in days; default 90.",
        ),
    ] = _RENEWAL_WINDOW_DEFAULT_DAYS,
    flash: Annotated[
        str | None,
        Query(
            description=(
                "Optional flash message rendered above the table after a "
                "successful POST /ui/renewals/{merchant_id}/attest redirect."
            ),
        ),
    ] = None,
) -> HTMLResponse:
    """Operator-visibility calendar of merchants approaching maturity.

    Rows derive from ``MerchantRepository.list_all()`` filtered to
    ``is_renewal=True`` whose ``maturity_date`` falls within
    ``window_days``. Sorted by ``days_until_maturity`` ascending (most
    urgent first). When the ``maturity_date`` column is absent from the
    schema (current state — see ``list_upcoming_renewals`` docstring),
    the accessor returns an empty list and the template renders an
    explicit "schema augmentation pending" empty state instead of a
    misleading "no rows" message.

    The ``attestations_repo`` consumes ``funder_renewal_attestations``
    (migration 040 / U6) to flip per-row ``renewal_status`` off the
    default ``not_required_funder_owns`` when the operator has captured
    an attestation that the funder transmitted the required notice.
    """
    rows = list_upcoming_renewals(
        merchants_repo, window_days=window_days, attestations=attestations_repo
    )
    # Detect the schema-augmentation gap so the template can render a
    # different empty state than the legitimate "no merchants in window"
    # case. Mirrors the accessor's own gap-detection: if any merchant in
    # the repo carries a real ``maturity_date`` attribute, the schema is
    # present and the empty result truly means "nobody's maturing soon."
    schema_missing = not any(
        isinstance(getattr(m, "maturity_date", None), date)
        and not isinstance(getattr(m, "maturity_date", None), datetime)
        for m in merchants_repo.list_all()
    )
    # Feature 3 (2026-06-15) — renewal-pipeline queue. Separate from the
    # 90-day attestation calendar above: the pipeline is the 14-day
    # "merchants approaching maturity — re-engage for renewal" view.
    # Implementation choice (b) — derive from ``maturity_date``
    # directly (``funding_date + term_days = maturity_date`` is the
    # same predicate). Option (a) — adding ``funding_date`` + ``term_days``
    # columns to merchants — is deferred until the operator wants to
    # surface them as separate columns. See ``list_renewal_pipeline``
    # docstring for the rationale.
    pipeline_rows = list_renewal_pipeline(merchants_repo)
    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "renewals.html.j2",
            {
                "active": "Renewals",
                "rows": rows,
                "pipeline_rows": pipeline_rows,
                "window_days": window_days,
                "schema_missing": schema_missing,
                "flash": flash,
            },
        ),
    )


_RENEWAL_ATTESTATION_NOTES_MAX_LEN: Final[int] = 2000


@router.post("/renewals/{merchant_id}/attest", response_model=None)
async def renewal_attestation_submit(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    attestations: Annotated[
        RenewalAttestationRepository,
        Depends(get_renewal_attestation_repository),
    ],
    audit: Annotated[AuditLog, Depends(get_audit)],
    funder_name: Annotated[str, Form()],
    disclosure_sent_at: Annotated[str, Form()],
    maturity_date_form: Annotated[str, Form(alias="maturity_date")],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    notes: Annotated[str, Form()] = "",
) -> Response:
    """Record one operator attestation that the funder sent the renewal notice.

    The merchant must exist + be a finalized renewal row with the same
    ``maturity_date`` as the one the operator is attesting against. The
    state and statute are derived from the merchant + the lookup table
    in ``aegis.merchants.renewal_attestations`` — the operator does NOT
    enter them so the row is consistent with what the calendar surfaced.

    Returns a 303 redirect back to ``/ui/renewals`` with a flash message
    on success, a 400 on bad input, a 404 on unknown merchant, and a
    409 when an attestation for the same (merchant, maturity, funder)
    already exists (see ``RenewalAttestationConflictError`` rationale).
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not merchant.is_renewal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant is not flagged as a renewal",
        )
    if merchant.maturity_date is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no maturity_date — set it before attesting",
        )
    if merchant.state is None or len(merchant.state) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no state — set it before attesting",
        )

    # Parse + validate the date inputs. The form supplies maturity_date
    # back so the route can verify the operator is attesting against the
    # maturity they saw on the calendar (defensive against a stale form
    # render after the operator edited the merchant in another tab).
    try:
        parsed_maturity = date.fromisoformat(maturity_date_form.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid maturity_date: {maturity_date_form!r}",
        ) from exc
    if parsed_maturity != merchant.maturity_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "submitted maturity_date does not match merchant maturity_date "
                "— reload the renewal calendar and retry"
            ),
        )

    try:
        parsed_sent_at = date.fromisoformat(disclosure_sent_at.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid disclosure_sent_at: {disclosure_sent_at!r}",
        ) from exc

    cleaned_funder = funder_name.strip()
    if not cleaned_funder:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="funder_name must not be empty",
        )
    if len(cleaned_funder) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="funder_name exceeds 255 characters",
        )

    cleaned_notes = notes.strip()
    if len(cleaned_notes) > _RENEWAL_ATTESTATION_NOTES_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"notes exceeds {_RENEWAL_ATTESTATION_NOTES_MAX_LEN} characters"),
        )

    try:
        record_renewal_attestation(
            attestations,
            audit,
            merchant_id=merchant.id,
            funder_name=cleaned_funder,
            maturity_date=merchant.maturity_date,
            disclosure_sent_at=parsed_sent_at,
            attested_by=actor_email or "dashboard",
            state=merchant.state,
            actor_email=actor_email,
            notes=cleaned_notes or None,
        )
    except RenewalAttestationConflictError as exc:
        # 409: duplicate attestation. The operator sees the conflict
        # message in the rendered HTTPException response; the row is
        # NOT written and no audit entry is recorded.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RenewalAttestationWriteError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    flash_msg = f"Recorded {cleaned_funder} attestation for {parsed_sent_at.isoformat()}."
    return RedirectResponse(
        url=f"/ui/renewals?flash={urllib.parse.quote(flash_msg)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
