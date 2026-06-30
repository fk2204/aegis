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
    get_close_client,
    get_funder_note_submission_repository,
    get_funder_reply_repository,
    get_funder_repository,
    get_merchant_repository,
)
from aegis.audit import AuditLog
from aegis.close.client import CloseClient, CloseError
from aegis.config import get_settings
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
from aegis.logger import get_logger
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.ops.operators import resolve_operator_email
from aegis.web._templates import templates

_log = get_logger(__name__)

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


# Outcome → Close opportunity status mapping. Only approved / declined
# flip the status; countered + no_response leave the opportunity where
# it is (the operator is still negotiating).
_OUTCOME_TO_CLOSE_STATUS: dict[ReplyOutcome, str] = {
    "approved": "close_funded_status_id",
    "declined": "close_dead_lender_status_id",
}


def _format_outcome_note(
    *,
    funder_name: str,
    outcome: ReplyOutcome,
    amount: Decimal | None,
    factor_rate: Decimal | None,
    term_days: int | None,
    notes: str | None,
    actor_email: str,
) -> str:
    """Build the Close-note body that mirrors the AEGIS outcome capture.

    Plain text, no PII beyond the merchant's own funder context. Used
    by ``_sync_outcome_to_close`` after a successful ``record_outcome``.
    """
    lines = [f"AEGIS funder outcome: {funder_name} — {outcome}"]
    if amount is not None:
        lines.append(f"  amount: ${amount}")
    if factor_rate is not None:
        lines.append(f"  factor rate: {factor_rate}")
    if term_days is not None:
        lines.append(f"  term: {term_days} days")
    if notes:
        lines.append(f"  notes: {notes}")
    lines.append(f"  recorded by: {actor_email}")
    return "\n".join(lines)


def _sync_outcome_to_close(
    *,
    merchant_id: UUID,
    funder_id: UUID,
    submission_id: UUID,
    outcome: ReplyOutcome,
    outcome_amount: Decimal | None,
    outcome_factor_rate: Decimal | None,
    outcome_term_days: int | None,
    outcome_notes: str | None,
    merchants_repo: MerchantRepository,
    funder_repo: FunderRepository,
    close_client: CloseClient | None,
    audit: AuditLog,
    actor_email: str,
) -> None:
    """Best-effort Close mirror of a funder-reply outcome.

    Two writes when the merchant has a Close lead AND opportunity:
      1. ``POST /activity/note/`` summarising the outcome.
      2. ``PUT /opportunity/{id}/`` flipping ``status_id`` to
         Funded (approved) or Dead-Lender (declined).

    ``countered`` and ``no_response`` post the note but never change
    the opportunity status — the operator is still negotiating.

    All failures are audited (``close.outcome_sync.failed``) and logged
    but never re-raised: the AEGIS ``funder_replies`` row is the
    authoritative outcome state. A Close hiccup must not block the
    operator's capture.
    """
    if close_client is None:
        return
    try:
        merchant = merchants_repo.get(merchant_id)
    except MerchantNotFoundError:
        _log.warning(
            "close.outcome_sync.merchant_missing merchant_id=%s submission_id=%s",
            merchant_id, submission_id,
        )
        return
    if not merchant.close_lead_id:
        return
    funder_name = _resolve_funder_name(funder_repo, funder_id)
    note_text = _format_outcome_note(
        funder_name=funder_name,
        outcome=outcome,
        amount=outcome_amount,
        factor_rate=outcome_factor_rate,
        term_days=outcome_term_days,
        notes=outcome_notes,
        actor_email=actor_email,
    )
    try:
        close_client.post_note(merchant.close_lead_id, note_text)
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="close.outcome_note_posted",
            subject_type="funder_note_submission",
            subject_id=submission_id,
            details={"close_lead_id": merchant.close_lead_id, "outcome": outcome},
        )
    except CloseError as exc:
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="close.outcome_sync.failed",
            subject_type="funder_note_submission",
            subject_id=submission_id,
            details={"stage": "note", "error": str(exc)[:300]},
        )
        _log.warning(
            "close.outcome_note_post_failed submission_id=%s err=%s",
            submission_id, exc,
        )
    settings_attr = _OUTCOME_TO_CLOSE_STATUS.get(outcome)
    if settings_attr is None or not merchant.close_opportunity_id:
        return
    target_status_id = getattr(get_settings(), settings_attr)
    try:
        close_client.update_opportunity_status(
            merchant.close_opportunity_id, target_status_id
        )
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="close.opportunity_status_synced",
            subject_type="funder_note_submission",
            subject_id=submission_id,
            details={
                "close_opportunity_id": merchant.close_opportunity_id,
                "status_id": target_status_id,
                "outcome": outcome,
            },
        )
    except CloseError as exc:
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="close.outcome_sync.failed",
            subject_type="funder_note_submission",
            subject_id=submission_id,
            details={"stage": "status", "error": str(exc)[:300]},
        )
        _log.warning(
            "close.opportunity_status_sync_failed submission_id=%s err=%s",
            submission_id, exc,
        )


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
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    close_client: Annotated[CloseClient | None, Depends(get_close_client)],
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
            submission_id=submission_id,
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

    # 2026-06-30 — Close outbound sync. After the funder_replies row
    # lands, mirror the outcome back to the Close lead: post a note
    # summarizing the decision + flip the opportunity status_id to
    # Funded / Dead-Lender when approved / declined. Best-effort: any
    # Close-side error audits + logs but never fails the operator's
    # outcome capture (the funder_replies write is the authoritative
    # AEGIS state; Close mirroring is convenience for the CRM view).
    _sync_outcome_to_close(
        merchant_id=merchant_id,
        funder_id=funder_id,
        submission_id=submission_id,
        outcome=typed_outcome,
        outcome_amount=amount,
        outcome_factor_rate=factor_rate,
        outcome_term_days=term_days,
        outcome_notes=notes_normalized,
        merchants_repo=merchants_repo,
        funder_repo=funder_repo,
        close_client=close_client,
        audit=audit,
        actor_email=recorded_by,
    )

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
