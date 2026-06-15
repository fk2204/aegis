"""Submissions sub-router — operator updates to funder_note_submissions.

One route: ``PATCH /ui/submissions/{submission_id}/status``. Reached
from the dossier's § 5½ "Funder submissions" block; HTMX swaps the
row in place with the updated server-rendered partial.

The route does NOT live in ``merchants.py`` because the URL path is
keyed by ``submission_id`` (not merchant_id) — a future bulk-update
or per-funder workflow would land at ``/ui/submissions/...`` too, so
keeping the URL family on its own router avoids the dossier router
growing further. Same Cloudflare Access posture as every other ``/ui``
route.

Form-input policy (matches the partial in
``templates/_submission_row.html.j2``):

* Empty inputs are treated as "leave the stored value alone" so the
  operator can edit one field without re-typing the others.
* Numeric fields are parsed via ``Decimal(str)`` — never ``float`` —
  per CLAUDE.md money-math discipline.
* ``status`` must be one of the four ``FunderNoteSubmissionStatus``
  literal values; anything else returns 400 (the dropdown can't
  produce other values, but a hand-crafted POST could).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi import status as http_status
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_funder_note_submission_repository,
)
from aegis.audit import AuditLog
from aegis.funder_note_submissions.models import (
    FunderNoteSubmissionRow,
    FunderNoteSubmissionStatus,
)
from aegis.funder_note_submissions.repository import (
    FunderNoteSubmissionNotFoundError,
    FunderNoteSubmissionRepository,
)
from aegis.ops.operators import resolve_operator_email
from aegis.web._templates import templates

router = APIRouter()


_VALID_STATUSES: frozenset[str] = frozenset({"pending", "approved", "declined", "countered"})


def _parse_decimal_or_none(raw: str) -> Decimal | None:
    """Parse an optional Decimal from a form field. ``""`` / whitespace
    -> ``None`` (operator left the field blank — keep stored value).
    Bad input raises ``HTTPException(400)`` so the caller can surface
    the error to the operator."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return Decimal(stripped)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"invalid decimal input: {stripped!r}",
        ) from exc


def _render_row(row: FunderNoteSubmissionRow) -> HTMLResponse:
    """Render the _submission_row.html.j2 partial as an HTMX swap."""
    html = templates.get_template("_submission_row.html.j2").render(s=row)
    return HTMLResponse(content=html)


@router.patch("/submissions/{submission_id}/status", response_model=None)
async def update_submission_status(
    submission_id: UUID,
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    audit: Annotated[AuditLog, Depends(get_audit)],
    status: Annotated[str, Form()],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    offer_amount: Annotated[str, Form()] = "",
    offer_factor: Annotated[str, Form()] = "",
    offer_holdback: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Capture an operator response on one funder_note_submission row.

    Returns the server-rendered ``_submission_row.html.j2`` partial so
    HTMX can swap the row in place. On 404 / 400 the caller (the
    dossier form) renders the FastAPI default error response — no
    silent failure path.
    """
    status_normalized = status.strip().lower()
    if status_normalized not in _VALID_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=(f"status must be one of {sorted(_VALID_STATUSES)}; got {status!r}"),
        )
    typed_status: FunderNoteSubmissionStatus = status_normalized  # type: ignore[assignment]

    amount = _parse_decimal_or_none(offer_amount)
    factor = _parse_decimal_or_none(offer_factor)
    holdback = _parse_decimal_or_none(offer_holdback)
    notes_normalized = notes.strip() or None

    try:
        updated = funder_note_subs.update_status(
            submission_id,
            status=typed_status,
            offer_amount=amount,
            offer_factor=factor,
            offer_holdback=holdback,
            notes=notes_normalized,
        )
    except FunderNoteSubmissionNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="submission.status_updated",
        subject_type="merchant",
        subject_id=updated.merchant_id,
        details={
            "submission_id": str(submission_id),
            "funder_id": str(updated.funder_id),
            "status": updated.status,
            "offer_amount": str(updated.offer_amount) if updated.offer_amount else None,
            "offer_factor": str(updated.offer_factor) if updated.offer_factor else None,
            "offer_holdback": (str(updated.offer_holdback) if updated.offer_holdback else None),
            "notes_chars": len(updated.notes) if updated.notes else 0,
        },
    )

    return _render_row(updated)


__all__ = ["router"]
