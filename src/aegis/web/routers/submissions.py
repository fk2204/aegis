"""Submissions sub-router — operator updates + read-only history views.

Three routes:

* ``PATCH /ui/submissions/{submission_id}/status`` — capture a funder
  response on one row. Reached from the dossier's § 5½ "Funder
  submissions" block; HTMX swaps the row in place with the updated
  server-rendered partial.
* ``GET /ui/merchants/{merchant_id}/submissions`` — per-merchant
  history of every funder_note_submission for one merchant, newest
  first. Linked from each funder card in the dossier's inline § 4
  matched-funders panel (Phase 7C).
* ``GET /ui/submissions`` — portfolio-wide history across all
  merchants, newest first, client-side filterable by status / funder
  / submission window.

The PATCH route does NOT live in ``merchants.py`` because the URL
path is keyed by ``submission_id`` (not merchant_id). The two GET
routes live here for cohesion with the URL family — every read /
write surface for funder_note_submissions hangs off the same router.
Same Cloudflare Access posture as every other ``/ui`` route.

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

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
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
from aegis.funders.repository import FunderRepository
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.ops.operators import resolve_operator_email
from aegis.web._templates import templates

router = APIRouter()

_PORTFOLIO_LIST_LIMIT = 500
_PER_MERCHANT_LIST_LIMIT = 500

_STATUS_CHIP_CLASS: dict[FunderNoteSubmissionStatus, str] = {
    "approved": "pos",
    "declined": "bad",
    "countered": "warn",
    "pending": "",
}


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


# ---------------------------------------------------------------------------
# Phase 7C — read-only submission history surfaces
# ---------------------------------------------------------------------------


def _days_to_response(row: FunderNoteSubmissionRow) -> int | None:
    """Whole days between ``submitted_at`` and ``responded_at``. ``None``
    when the funder hasn't responded yet — the template renders that as
    an em-dash."""
    if row.responded_at is None:
        return None
    delta = row.responded_at - row.submitted_at
    return delta.days


def _serialize_row(
    row: FunderNoteSubmissionRow,
    funder_name: str,
) -> dict[str, object]:
    """Shape one submission row into the dict the templates render. The
    template only reads dict keys — no Pydantic round-trip needed."""
    return {
        "id": str(row.id),
        "merchant_id": str(row.merchant_id),
        "funder_id": str(row.funder_id),
        "funder_name": funder_name,
        "submitted_at": row.submitted_at,
        "submitted_at_iso": row.submitted_at.isoformat(),
        "submitted_at_date": row.submitted_at.date().isoformat(),
        "responded_at": row.responded_at,
        "status": row.status,
        "status_chip_class": _STATUS_CHIP_CLASS.get(row.status, ""),
        "offer_amount": row.offer_amount,
        "offer_factor": row.offer_factor,
        "offer_holdback": row.offer_holdback,
        "notes": row.notes,
        "days_to_response": _days_to_response(row),
    }


def _funder_name_map(funder_repo: FunderRepository) -> dict[UUID, str]:
    """Single SELECT against funders → ``{id: name}``. Used to avoid
    N+1 lookups when rendering a submission list — the per-merchant
    view typically has <20 rows but may reference funders that are no
    longer active. Inactive funders fall back to the row's funder_id
    truncation in the template."""
    return {f.id: f.name for f in funder_repo.list_active()}


def _month_window_utc(now: datetime) -> tuple[datetime, datetime]:
    """Return ``(month_start, next_month_start)`` UTC for the calendar
    month containing ``now``. Used to count approved/declined within
    the current month on the portfolio view."""
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return start, end


@router.get("/merchants/{merchant_id}/submissions", response_class=HTMLResponse)
async def merchant_submissions_view(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
) -> HTMLResponse:
    """Per-merchant submission history (Phase 7C).

    Lists every funder_note_submission row for one merchant, newest
    first. Each row is anchored ``id="funder-{funder_id}"`` so the
    dossier's "View history" link can scroll to the specific funder.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    rows = funder_note_subs.list_for_merchant(merchant_id, limit=_PER_MERCHANT_LIST_LIMIT)
    funder_names = _funder_name_map(funder_repo)
    serialized = [
        _serialize_row(r, funder_names.get(r.funder_id, str(r.funder_id)[:8])) for r in rows
    ]

    return templates.TemplateResponse(
        request,
        "merchant_submissions.html.j2",
        {
            "merchant": merchant,
            "submissions": serialized,
        },
    )


@router.get("/submissions", response_class=HTMLResponse)
async def portfolio_submissions_view(
    request: Request,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
) -> HTMLResponse:
    """Portfolio-wide submission history (Phase 7C).

    Lists the most recent ``_PORTFOLIO_LIST_LIMIT`` submissions across
    every merchant, newest first. Top of page shows pending /
    approved-this-month / declined-this-month counters. Filtering
    (status chips, funder dropdown, date toggle) is purely client-side
    — JS reads ``data-status`` / ``data-funder-id`` /
    ``data-submitted-at`` attributes and toggles row visibility.
    """
    rows = funder_note_subs.list_recent(limit=_PORTFOLIO_LIST_LIMIT)
    funder_names = _funder_name_map(funder_repo)

    # Merchant business_name lookup — batch via list_all() so the
    # template doesn't N+1. Inactive merchants are still listed (the
    # submissions outlive merchant edits).
    merchant_name_by_id: dict[UUID, str] = {m.id: m.business_name for m in merchants.list_all()}

    serialized = [
        _serialize_row(r, funder_names.get(r.funder_id, str(r.funder_id)[:8])) for r in rows
    ]
    for s, r in zip(serialized, rows, strict=True):
        s["merchant_name"] = merchant_name_by_id.get(r.merchant_id, str(r.merchant_id)[:8])

    # Counter math. Pending uses the in-window count (so the operator
    # sees the live backlog); approved/declined use month-to-date so a
    # year-end retrospective renders meaningfully even when most rows
    # are older than 30 days.
    now = datetime.now(UTC)
    month_start, month_end = _month_window_utc(now)
    pending_count = sum(1 for r in rows if r.status == "pending")
    approved_month = sum(
        1 for r in rows if r.status == "approved" and month_start <= r.submitted_at < month_end
    )
    declined_month = sum(
        1 for r in rows if r.status == "declined" and month_start <= r.submitted_at < month_end
    )

    # Distinct funder names that actually appear in the current row set
    # — drives the filter dropdown. Sorted alphabetically for stable
    # operator scan order.
    distinct_funders = sorted(
        {(s["funder_id"], s["funder_name"]) for s in serialized},
        key=lambda pair: str(pair[1]).lower(),
    )

    return templates.TemplateResponse(
        request,
        "portfolio_submissions.html.j2",
        {
            "submissions": serialized,
            "pending_count": pending_count,
            "approved_month_count": approved_month,
            "declined_month_count": declined_month,
            "distinct_funders": distinct_funders,
            "limit": _PORTFOLIO_LIST_LIMIT,
        },
    )


__all__ = ["router"]
