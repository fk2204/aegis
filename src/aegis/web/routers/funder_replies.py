"""Funder-reply sub-router — operator "Record outcome" capture.

Two routes:

* ``GET /ui/funder-replies/outcome-modal`` — returns the HTMX modal
  partial pre-populated with the funder name + submission identity.
  Reached from the "Record outcome" button on each
  ``_submission_row.html.j2``.
* ``POST /ui/funder-replies/outcome`` — persists one row to the
  ``funder_replies`` table (migration 071 outcome columns), writes
  the ``funder_reply.outcome_recorded`` audit row, and returns the
  refreshed submission row HTML so HTMX can swap it in place.

The capture writes the operator-recorded outcome — distinct from the
email-parse path in ``aegis.funders.replies.ingest_reply``. Same
``funder_replies`` table, different population strategy: the
``outcome_*`` columns are populated and ``raw_text`` / ``status`` stay
NULL (no email content). The DB CHECK constraint
``funder_replies_reply_or_outcome_check`` enforces that every row has
either a parsed reply or a recorded outcome.

Money / rate fields are parsed via ``Decimal(str)`` — never ``float`` —
per CLAUDE.md money-math discipline. Bad input returns 400 so the
form surfaces the error inline rather than silently writing a bogus
value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_funder_note_submission_repository,
    get_funder_reply_repository,
    get_funder_repository,
)
from aegis.audit import AuditLog
from aegis.funder_note_submissions.repository import (
    FunderNoteSubmissionNotFoundError,
    FunderNoteSubmissionRepository,
)
from aegis.funders.replies import (
    FunderReplyError,
    FunderReplyOutcomePayload,
    FunderReplyRepository,
    ReplyOutcome,
    record_outcome,
)
from aegis.funders.repository import FunderRepository
from aegis.ops.operators import resolve_operator_email
from aegis.web._templates import templates

router = APIRouter()


_VALID_OUTCOMES: frozenset[str] = frozenset({"approved", "declined", "countered", "no_response"})
_OUTCOMES_WITH_OFFER: frozenset[str] = frozenset({"approved", "countered"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_decimal_or_none(raw: str) -> Decimal | None:
    """Parse an optional Decimal from a form field. ``""`` / whitespace
    -> ``None``. Bad input raises 400 so the operator sees the failure
    inline rather than silently writing a stale value."""
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


def _parse_int_or_none(raw: str) -> int | None:
    """Parse an optional int from a form field. ``""`` / whitespace -> ``None``.
    Bad input raises 400 to mirror ``_parse_decimal_or_none``."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"invalid integer input: {stripped!r}",
        ) from exc


def _resolve_funder_name(
    funder_repo: FunderRepository,
    funder_id: UUID,
) -> str:
    """Lookup the funder name for the modal header. Falls back to the
    truncated UUID when the funder is no longer active or has been
    archived — submissions outlive funder edits."""
    for f in funder_repo.list_active():
        if f.id == funder_id:
            return f.name
    return f"funder {str(funder_id)[:8]}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/funder-replies/outcome-modal",
    response_class=HTMLResponse,
)
async def outcome_modal(
    request: Request,
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    merchant_id: Annotated[UUID, Query()],
    funder_id: Annotated[UUID, Query()],
    submission_id: Annotated[UUID, Query()],
) -> HTMLResponse:
    """Render the Record-outcome modal partial.

    The modal is purely a form; persistence happens on POST
    ``/ui/funder-replies/outcome``. We pre-fill the funder name +
    identity so the operator sees what they're recording against.
    """
    funder_name = _resolve_funder_name(funder_repo, funder_id)
    return templates.TemplateResponse(
        request,
        "funder_reply_outcome_modal.html.j2",
        {
            "merchant_id": merchant_id,
            "funder_id": funder_id,
            "submission_id": submission_id,
            "funder_name": funder_name,
        },
    )


@router.post(
    "/funder-replies/outcome",
    response_class=HTMLResponse,
)
async def record_funder_reply_outcome(
    request: Request,
    reply_repo: Annotated[FunderReplyRepository, Depends(get_funder_reply_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchant_id: Annotated[UUID, Form()],
    funder_id: Annotated[UUID, Form()],
    submission_id: Annotated[UUID, Form()],
    outcome: Annotated[str, Form()],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    outcome_amount: Annotated[str, Form()] = "",
    outcome_factor_rate: Annotated[str, Form()] = "",
    outcome_term_days: Annotated[str, Form()] = "",
    outcome_notes: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Persist the operator-recorded outcome and return the refreshed row.

    Validation:

      * ``outcome`` must be one of {approved, declined, countered,
        no_response}; anything else returns 400.
      * For ``outcome in {declined, no_response}`` the offer fields
        (amount / factor / term_days) are silently dropped — the
        modal's onchange handler already clears them, but a hand-
        crafted POST might not. Mirrors the DB CHECK so the request
        fails predictably either way.
      * ``submission_id`` MUST resolve to a known
        funder_note_submission; 404 otherwise. Prevents writing an
        outcome row that has no surface to render on.

    Identity:

      * ``outcome_recorded_by`` falls back to ``"dashboard"`` when no
        CF-Access email header is present (local dev). In prod the
        SSO gate guarantees the email is set; the fallback exists for
        the test client + local curl path.
    """
    # Validate the dropdown value.
    outcome_normalized = outcome.strip().lower()
    if outcome_normalized not in _VALID_OUTCOMES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=(f"outcome must be one of {sorted(_VALID_OUTCOMES)}; got {outcome!r}"),
        )
    typed_outcome: ReplyOutcome = outcome_normalized  # type: ignore[assignment]

    # Ensure the submission row exists so the HTMX swap target is real.
    try:
        submission = funder_note_subs.get(submission_id)
    except FunderNoteSubmissionNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    # Defensive identity check: the modal POST should describe the
    # same merchant/funder pair as the submission row it references.
    # A mismatch suggests a stale modal that was opened before the
    # submission was edited, or a hand-crafted POST. Fail fast.
    if submission.merchant_id != merchant_id or submission.funder_id != funder_id:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=(
                "submission_id does not match merchant_id/funder_id — form payload is inconsistent"
            ),
        )

    # Parse the optional offer fields. For declined / no_response we
    # drop them explicitly so an inconsistent client POST cannot ride
    # through the Pydantic gate (the model rejects offer fields on
    # those outcomes, but dropping here surfaces the right HTTP
    # behavior to the form even when the JS handler didn't clear).
    if typed_outcome in _OUTCOMES_WITH_OFFER:
        amount = _parse_decimal_or_none(outcome_amount)
        factor_rate = _parse_decimal_or_none(outcome_factor_rate)
        term_days = _parse_int_or_none(outcome_term_days)
    else:
        amount = None
        factor_rate = None
        term_days = None

    notes_normalized = outcome_notes.strip() or None
    recorded_by = (actor_email or "dashboard").strip()
    if not recorded_by:
        recorded_by = "dashboard"

    try:
        payload = FunderReplyOutcomePayload(
            deal_id=submission_id,  # we record against the submission row
            funder_id=funder_id,
            outcome=typed_outcome,
            outcome_amount=amount,
            outcome_factor_rate=factor_rate,
            outcome_term_days=term_days,
            outcome_notes=notes_normalized,
            outcome_recorded_by=recorded_by,
        )
    except ValueError as exc:
        # Pydantic validation failure — surface as 400 with the
        # validation message so the form displays the actual gap.
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        record_outcome(
            payload,
            repo=reply_repo,
            audit=audit,
            now=datetime.now(UTC),
        )
    except FunderReplyError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"funder_reply_persist_unavailable: {exc}",
        ) from exc

    # Re-fetch the submission row so the swap surfaces any updates the
    # repository made (currently only updated_at on this surface — the
    # outcome itself lives on funder_replies, not the submission row).
    refreshed = funder_note_subs.get(submission_id)
    funder_name = _resolve_funder_name(funder_repo, funder_id)
    html = templates.get_template("_submission_row.html.j2").render(
        s=_RowAdapter(refreshed, funder_name=funder_name),
    )
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# View adapter — keep _submission_row.html.j2 untouched
# ---------------------------------------------------------------------------


class _RowAdapter:
    """Thin adapter giving ``_submission_row.html.j2`` the fields it
    reads off the submission row. The template only touches ``id``,
    ``submitted_at``, ``funder_id``, ``status``, ``offer_amount`` /
    ``factor`` / ``holdback``, ``notes``, ``responded_at``, and
    ``merchant_id`` — wrapping the row keeps this route from leaking
    into the model layer.

    A dataclass / Pydantic adapter would be heavier than necessary;
    Jinja just reads attributes, so a plain attribute proxy is the
    minimum surface that satisfies the contract.
    """

    __slots__ = ("_row", "funder_name")

    def __init__(self, row: object, *, funder_name: str) -> None:
        self._row = row
        self.funder_name = funder_name

    def __getattr__(self, name: str) -> object:
        return getattr(self._row, name)


__all__ = ["router"]
