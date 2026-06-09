"""/disclosures — render the state-prescribed disclosure for a deal.

Routes a deal's state to the disclosure renderer:

  * Tier 1 → renders the regulator's prescribed Jinja template.
  * Tier 2 → renders the generic acknowledgment citing general law.
  * Tier 3 (unaudited) → 503 ``state_not_audited``. The operator must
    complete the audit before AEGIS can disclose for this state.
  * Non-served state → 422 ``state_not_served``.
  * APR computation failure (``APRDisclosureError``) → 503
    ``apr_compute_failed`` with ``disclosure_status="needs_review"``.
    The deal is held for operator review; AEGIS NEVER ships a disclosure
    with a missing or zero APR. This is internal pre-flight gating —
    funder partners own the regulator-facing issuance, but the silent
    0.00% fallback that previously lived in the context builder was a
    CA DFPI §§ 940/942 material defect and is now an explicit halt.

Returns ``Content-Type: text/html`` so the operator can preview directly,
plus a JSON wrapper endpoint for programmatic callers (Close sync,
dashboard).

R0.4 caller plumbing notes
--------------------------
``APRDisclosureError`` carries the deal context (state, principal, factor,
term_days, disbursement_date, deal_id). When caught here we:

  1. Write one ``audit_log`` row with ``action='aegis_apr_compute_failed'``,
     ``subject_type='deal'``, and the numeric inputs in ``details``.
     CRITICAL — per CLAUDE.md PII rules the audit ``details`` JSONB must
     NOT contain merchant business names, owner names, transaction
     descriptions, or any other PII. Only the deal-id-ish reference + the
     numeric inputs that failed.
  2. Return 503 with a structured detail so the dashboard/Close sync can
     surface a "held for operator review" state to the operator instead
     of crashing.

U16 persistence (migration 042)
-------------------------------
R0.4 / U3 deferred persisting the ``disclosure_status``; U16 closes
that loop. ``disclosure_render_events`` (migration 042) is the
internal pre-flight render log:

  * Happy-path render → ``record_disclosure_render_event(...)`` writes
    one row with ``status='ok'``.
  * APR failure → after the existing U3 audit_log write (which stays
    per CLAUDE.md "audit-write failures FAIL the operation"), we ALSO
    write one render-event row with ``status='apr_compute_failed'``
    and the same non-PII details. The render-event helper also writes
    a paired ``aegis_disclosure_render_event`` audit_log row so the
    durable audit trail captures the event regardless of which side a
    reader queries from.

The U3 audit row (action='aegis_apr_compute_failed') and the U16 audit
row (action='aegis_disclosure_render_event') are deliberately distinct:
U3's row is the existing audit contract; U16's row is the structured
render-event signal. Both fire on the failure path; only U16's fires
on the success path.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit, get_disclosure_render_event_repository
from aegis.audit import AuditLog
from aegis.compliance.disclosure import (
    APRDisclosureError,
    RenderedDisclosure,
    render_disclosure,
)
from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_OK,
    DisclosureRenderEventRepository,
    record_disclosure_render_event,
)
from aegis.compliance.states import StateNotAudited, StateNotServed
from aegis.logger import get_logger
from aegis.ops.operators import resolve_operator_email
from aegis.scoring.models import ScoreInput, ScoreResult

_log = get_logger(__name__)

router = APIRouter(
    prefix="/disclosures",
    tags=["disclosures"],
    dependencies=[Depends(require_bearer)],
)


_APR_NEEDS_REVIEW_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Disclosure held for operator review</title></head>
<body>
<h1>Disclosure held for operator review</h1>
<p>The APR for this deal could not be computed from the supplied payment
schedule. AEGIS will not render a disclosure with a missing or zero APR.</p>
<p><strong>Status:</strong> <code>needs_review</code></p>
<p>This is an internal pre-flight gate; the funder owns the
regulator-facing disclosure. Resolve the upstream payment-schedule or
factor/term inputs and retry, or escalate to the operator for review.</p>
</body></html>
"""


class DisclosureRequest(BaseModel):
    """Body for /disclosures/render — the deal + score the disclosure cites."""

    model_config = ConfigDict(extra="forbid")

    state: str
    deal: ScoreInput
    score: ScoreResult


class DisclosureRenderResponse(BaseModel):
    """Wraps ``RenderedDisclosure`` with an in-memory disclosure_status.

    ``disclosure_status`` is "ok" on the happy path; the needs_review path
    never returns this model (it raises 503 with a structured detail).
    The field exists here so the dashboard can render a consistent shape
    and so a future persistence ticket has a clear payload to model.
    """

    model_config = ConfigDict(extra="forbid")

    disclosure_status: Literal["ok"] = "ok"
    rendered: RenderedDisclosure


def _apr_failure_details(exc: APRDisclosureError) -> dict[str, object]:
    """Build the PII-safe details payload shared by audit + render-event rows.

    Only deal-id-ish references + numeric APR inputs. No business_name /
    owner_name / transaction descriptions per CLAUDE.md PII rules.
    """
    return {
        "deal_id": exc.deal_id,
        "state": exc.state,
        "principal": str(exc.principal) if exc.principal is not None else None,
        "factor": str(exc.factor) if exc.factor is not None else None,
        "term_days": exc.term_days,
        "disbursement_date": (
            exc.disbursement_date.isoformat()
            if exc.disbursement_date is not None
            else None
        ),
        "reason": str(exc),
    }


def _deal_subject_uuid(deal_id: str | None) -> UUID | None:
    """Parse a deal_id string to UUID, or None on non-UUID / missing input.

    Mirrors the original ``_audit_apr_failure`` behavior so callers can
    use the same subject_id resolution for both the audit_log row and
    the render-event row.
    """
    if deal_id is None:
        return None
    try:
        return UUID(deal_id)
    except ValueError:
        return None


def _audit_apr_failure(
    audit: AuditLog,
    exc: APRDisclosureError,
    *,
    actor_email: str | None,
) -> None:
    """Persist one ``aegis_apr_compute_failed`` audit row.

    PII-safe: ``details`` contains only deal-id-ish references and the
    numeric APR inputs that failed. No business_name / owner_name /
    transaction descriptions.
    """
    audit.record(
        actor="api",
        actor_email=actor_email,
        action="aegis_apr_compute_failed",
        subject_type="deal",
        subject_id=_deal_subject_uuid(exc.deal_id),
        details=_apr_failure_details(exc),
    )


def _record_apr_failure_event(
    repo: DisclosureRenderEventRepository,
    audit: AuditLog,
    exc: APRDisclosureError,
    *,
    actor_email: str | None,
) -> None:
    """Persist the U16 render-event row alongside U3's audit_log write.

    Called AFTER ``_audit_apr_failure`` so U3's contract still fires
    first; this is an additive write, not a replacement.
    """
    details = _apr_failure_details(exc)
    record_disclosure_render_event(
        repo,
        audit,
        deal_id=_deal_subject_uuid(exc.deal_id),
        merchant_id=None,
        state=exc.state,
        template_path=None,
        status=RENDER_EVENT_STATUS_APR_FAILED,
        status_reason=str(exc)[:255],
        details=details,
        recipient_email=None,
        rendered_by="api",
        actor="api",
        actor_email=actor_email,
    )


def _record_ok_render_event(
    repo: DisclosureRenderEventRepository,
    audit: AuditLog,
    req: DisclosureRequest,
    rendered: RenderedDisclosure,
    *,
    actor_email: str | None,
) -> None:
    """Persist the U16 happy-path render-event row.

    Only non-PII context: state, tier, and the deal's merchant_id. No
    business_name / owner_name / transaction descriptions per CLAUDE.md
    PII rules. ``RenderedDisclosure`` does not surface ``template_path``
    on the public model — the render event records the resolved tier
    instead, which the operator can map back to a template via the
    state matrix.
    """
    merchant_uuid: UUID | None = req.deal.merchant_id
    details: dict[str, object] = {
        "deal_id": str(merchant_uuid) if merchant_uuid is not None else None,
        "state": req.state,
        "tier": rendered.tier,
    }
    record_disclosure_render_event(
        repo,
        audit,
        deal_id=merchant_uuid,
        merchant_id=merchant_uuid,
        state=req.state,
        template_path=None,
        status=RENDER_EVENT_STATUS_OK,
        status_reason=None,
        details=details,
        recipient_email=None,
        rendered_by="api",
        actor="api",
        actor_email=actor_email,
    )


def _render_or_raise(req: DisclosureRequest) -> RenderedDisclosure:
    try:
        return render_disclosure(req.state, req.deal, req.score)
    except StateNotServed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except StateNotAudited as exc:
        # 503 because the operator can resolve this by completing the audit;
        # it's a configuration absence, not a client error.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"state_not_audited: {exc.state}",
        ) from exc


@router.post(
    "/render",
    response_model=DisclosureRenderResponse,
    summary="Render a disclosure as JSON (state, tier, html, citation).",
)
def render_disclosure_json(
    req: DisclosureRequest,
    audit: Annotated[AuditLog, Depends(get_audit)],
    render_events: Annotated[
        DisclosureRenderEventRepository,
        Depends(get_disclosure_render_event_repository),
    ],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> DisclosureRenderResponse:
    try:
        rendered = _render_or_raise(req)
    except APRDisclosureError as exc:
        # U3: audit_log row stays per CLAUDE.md audit contract.
        _audit_apr_failure(audit, exc, actor_email=actor_email)
        # U16: additionally persist the render-event row (migration 042).
        _record_apr_failure_event(
            render_events, audit, exc, actor_email=actor_email
        )
        _log.warning(
            "disclosures.apr_compute_failed state=%s term_days=%s",
            exc.state,
            exc.term_days,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "apr_compute_failed",
                "disclosure_status": "needs_review",
                "message": (
                    "APR computation could not converge for this deal's "
                    "payment schedule. Disclosure is being held for "
                    "operator review."
                ),
                "state": exc.state,
                "term_days": exc.term_days,
            },
        ) from exc
    # U16: happy-path render-event row.
    _record_ok_render_event(
        render_events, audit, req, rendered, actor_email=actor_email
    )
    return DisclosureRenderResponse(rendered=rendered)


@router.post(
    "/render.html",
    response_class=HTMLResponse,
    summary="Render a disclosure and return raw HTML (text/html).",
)
def render_disclosure_html(
    req: DisclosureRequest,
    audit: Annotated[AuditLog, Depends(get_audit)],
    render_events: Annotated[
        DisclosureRenderEventRepository,
        Depends(get_disclosure_render_event_repository),
    ],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> HTMLResponse:
    try:
        rendered = _render_or_raise(req)
    except APRDisclosureError as exc:
        # U3: audit_log row stays per CLAUDE.md audit contract.
        _audit_apr_failure(audit, exc, actor_email=actor_email)
        # U16: additionally persist the render-event row (migration 042).
        _record_apr_failure_event(
            render_events, audit, exc, actor_email=actor_email
        )
        _log.warning(
            "disclosures.apr_compute_failed state=%s term_days=%s",
            exc.state,
            exc.term_days,
        )
        # 503 + an operator-facing error page. We do NOT emit a disclosure
        # HTML with a missing/zero APR — that's the silent fallback the
        # R0.4 gate exists to prevent.
        return HTMLResponse(
            content=_APR_NEEDS_REVIEW_HTML,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"X-Disclosure-Status": "needs_review"},
        )
    # U16: happy-path render-event row.
    _record_ok_render_event(
        render_events, audit, req, rendered, actor_email=actor_email
    )
    return HTMLResponse(content=rendered.html, status_code=200)


__all__ = ["DisclosureRenderResponse", "DisclosureRequest", "router"]
