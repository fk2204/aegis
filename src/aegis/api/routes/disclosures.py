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

There is no ``disclosure_status`` enum in the codebase yet and R0.4
explicitly defers persisting the status. We surface it in-memory on the
response models below; introducing a persisted column / migration is a
separate ticket pending the operator's call on the persistence shape.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit
from aegis.audit import AuditLog
from aegis.compliance.disclosure import (
    APRDisclosureError,
    RenderedDisclosure,
    render_disclosure,
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
    # deal_id may be a merchant UUID string (current shape — see
    # APRDisclosureError docstring) or None when the failure happened
    # before merchant lookup.
    subject_id: UUID | None = None
    if exc.deal_id is not None:
        try:
            subject_id = UUID(exc.deal_id)
        except ValueError:
            # Not a UUID — keep subject_id None and stash the raw string
            # in details so the row is still queryable by hand.
            subject_id = None

    details: dict[str, object] = {
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

    audit.record(
        actor="api",
        actor_email=actor_email,
        action="aegis_apr_compute_failed",
        subject_type="deal",
        subject_id=subject_id,
        details=details,
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
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> DisclosureRenderResponse:
    try:
        rendered = _render_or_raise(req)
    except APRDisclosureError as exc:
        _audit_apr_failure(audit, exc, actor_email=actor_email)
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
    return DisclosureRenderResponse(rendered=rendered)


@router.post(
    "/render.html",
    response_class=HTMLResponse,
    summary="Render a disclosure and return raw HTML (text/html).",
)
def render_disclosure_html(
    req: DisclosureRequest,
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> HTMLResponse:
    try:
        rendered = _render_or_raise(req)
    except APRDisclosureError as exc:
        _audit_apr_failure(audit, exc, actor_email=actor_email)
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
    return HTMLResponse(content=rendered.html, status_code=200)


__all__ = ["DisclosureRenderResponse", "DisclosureRequest", "router"]
