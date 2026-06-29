"""Admin sub-router — operator validation surface for the text-layer
shadow probe v2.

Routes:
  * ``GET  /ui/admin/text-layer-probe-review``                  — list
    unreviewed disagreements + the "ready to flip" banner when the
    verdict counts cross the corpus threshold.
  * ``POST /ui/admin/text-layer-probe-review/{document_id}/verdict``
    — record one operator verdict + return an HTMX partial that
    removes the row from the table.
  * ``POST /ui/admin/text-layer-probe-review/flip-to-live`` — stub
    endpoint that returns 202 + a copy-pasteable instruction set the
    operator follows to flip the probe (env-var edit + systemd
    restart). The actual flip is a deploy concern, NOT a route
    concern, per CLAUDE.md "Decision-boundary changes — deliberate +
    shadow-first": the flip is a config change the operator runs
    out-of-band so it can never accidentally fire from the dashboard.

Auth
----
``/ui/admin/*`` lives under the ``/ui`` prefix that the parent
``aegis.web.router.router`` gates with
``Depends(current_operator)`` — every request resolves an SSO
identity before any route runs. The two POST handlers additionally
require the ADMIN role via ``Depends(admin_only)`` because flipping
a decision-boundary probe is an admin-only decision per the role
matrix in ``aegis.web._role_gate``.

Banner threshold
----------------
The "ready to flip" banner renders when the verdict counts cross BOTH
gates simultaneously:

  * ``v2_correct >= 10`` — enough adjudicated true positives
  * ``v1_correct <= 2``  — the operator has not flagged the new probe
                           as wrong more than twice across the corpus

These thresholds are read from module-level constants so a future
tune is a single edit; CLAUDE.md "shadow-first for ALL new scoring
rules" treats the flip as the production-shadow validation step.
"""

from __future__ import annotations

from typing import Annotated, Final, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse

from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_probe_review_repository,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.merchants.repository import MerchantRepository
from aegis.ops.operators import Operator
from aegis.probe_review import PROBE_TEXT_LAYER_V2, ProbeReviewRepository
from aegis.probe_review.models import Verdict
from aegis.probe_review.repository import collect_unreviewed_disagreements
from aegis.storage import DocumentRepository
from aegis.web._role_gate import admin_only
from aegis.web._templates import templates

router = APIRouter()


# Flip-readiness thresholds — see module docstring for the rationale.
_BANNER_V2_CORRECT_FLOOR: Final[int] = 10
_BANNER_V1_CORRECT_CEIL: Final[int] = 2


# Audit row action names land here so a future test asserting the
# audit shape doesn't have to grep through the route source.
_AUDIT_ACTION_VERDICT: Final[str] = "probe_review.verdict_recorded"
_AUDIT_ACTION_FLIP_REQUEST: Final[str] = "probe_review.flip_requested"


def _banner_ready(counts: dict[str, int]) -> bool:
    """Return True iff the verdict counts satisfy the flip-readiness gate.

    Both conditions must hold:
      * ``v2_correct >= _BANNER_V2_CORRECT_FLOOR``
      * ``v1_correct <= _BANNER_V1_CORRECT_CEIL``
    """
    v2 = counts.get("v2_correct", 0)
    v1 = counts.get("v1_correct", 0)
    return v2 >= _BANNER_V2_CORRECT_FLOOR and v1 <= _BANNER_V1_CORRECT_CEIL


@router.get("/admin/text-layer-probe-review", response_class=HTMLResponse)
async def text_layer_probe_review_view(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    repo: Annotated[ProbeReviewRepository, Depends(get_probe_review_repository)],
) -> HTMLResponse:
    """Render the unreviewed disagreement table + banner.

    Read-only on the verdict store. Walks the most-recent documents
    via the document repository (default 500 — well above AEGIS's
    ~100 deals/month cadence) and surfaces the ones carrying a
    ``[SHADOW] text_layer_probe_v2_disagrees`` flag the requesting
    operator has not yet adjudicated.
    """
    operator = _request_operator(request)
    rows = collect_unreviewed_disagreements(
        docs=docs,
        merchants=merchants,
        repo=repo,
        probe_name=PROBE_TEXT_LAYER_V2,
        operator_email=operator.email,
    )
    counts = repo.count_verdicts(PROBE_TEXT_LAYER_V2)

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "admin_probe_review.html.j2",
            {
                "active": "Admin",
                "probe_name": PROBE_TEXT_LAYER_V2,
                "rows": rows,
                "counts": counts,
                "banner_ready": _banner_ready(counts),
                "banner_v2_floor": _BANNER_V2_CORRECT_FLOOR,
                "banner_v1_ceil": _BANNER_V1_CORRECT_CEIL,
            },
        ),
    )


@router.post(
    "/admin/text-layer-probe-review/{document_id}/verdict",
    response_class=HTMLResponse,
)
async def text_layer_probe_review_verdict(
    document_id: Annotated[UUID, Path()],
    repo: Annotated[ProbeReviewRepository, Depends(get_probe_review_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    operator: Annotated[Operator, Depends(admin_only)],
    verdict: Annotated[str, Form()],
) -> HTMLResponse:
    """Record one operator verdict; return an empty HTMX partial.

    Form body:
        ``verdict=v2_correct`` or ``verdict=v1_correct``

    Idempotent on the schema's ``UNIQUE (document_id, probe_name,
    operator_email)`` — a second click from the same operator returns
    the existing row unchanged. The HTMX response is an empty string
    so the caller's ``hx-target`` (the row's ``<tr>``) swaps to a
    zero-content node and disappears from the table.
    """
    if verdict not in ("v2_correct", "v1_correct"):
        # Surface a 400 so the test suite + operator JS can see the
        # bad input. The HTMX caller never sends a malformed form in
        # the happy path; this is the explicit-validation backstop.
        return HTMLResponse(
            content="invalid verdict",
            status_code=400,
        )

    # FastAPI's Form() coerces the body field to ``str``; cast to the
    # repository's narrower ``Verdict`` Literal after the membership
    # check above has narrowed the run-time value.
    verdict_typed = cast(Verdict, verdict)
    row = repo.add_verdict(
        document_id=document_id,
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict=verdict_typed,
        operator_email=operator.email,
    )
    audit.record(
        actor="dashboard",
        actor_email=operator.email,
        action=_AUDIT_ACTION_VERDICT,
        subject_type="document",
        subject_id=document_id,
        details={
            "probe_name": PROBE_TEXT_LAYER_V2,
            "verdict": verdict_typed,
            "verdict_id": str(row.id),
        },
    )
    # Empty 200 body — HTMX swaps the row out by replacing the
    # <tr id="probe-row-{id}"> with nothing. The banner / counts
    # refresh on the next full page load.
    return HTMLResponse(content="", status_code=200)


@router.post(
    "/admin/text-layer-probe-review/flip-to-live",
    response_class=JSONResponse,
)
async def text_layer_probe_review_flip_to_live(
    audit: Annotated[AuditLog, Depends(get_audit)],
    operator: Annotated[Operator, Depends(admin_only)],
) -> JSONResponse:
    """Stub endpoint — record the flip request, return 202 + instructions.

    The actual probe flip is a config change in
    ``/etc/aegis/aegis.env`` plus a systemd restart of ``aegis-web``;
    AEGIS deliberately does NOT mutate environment files from a route
    handler (CLAUDE.md "Decision-boundary changes — deliberate +
    shadow-first"). The audit row + 202 acknowledge the operator's
    intent and give them the exact command sequence to run on the
    box.
    """
    audit.record(
        actor="dashboard",
        actor_email=operator.email,
        action=_AUDIT_ACTION_FLIP_REQUEST,
        subject_type="probe",
        subject_id=None,
        details={"probe_name": PROBE_TEXT_LAYER_V2},
    )
    return JSONResponse(
        status_code=202,
        content={
            "message": (
                "Flip request acknowledged; operator must edit "
                "`/etc/aegis/aegis.env` and bounce aegis-web."
            ),
            "probe_name": PROBE_TEXT_LAYER_V2,
            "audit_action": _AUDIT_ACTION_FLIP_REQUEST,
        },
    )


def _request_operator(request: Request) -> Operator:
    """Pull the resolved ``Operator`` off ``request.state``.

    ``current_operator`` runs at the ``/ui`` router level (see
    ``aegis.web.router.router``) and stashes the resolved operator on
    ``request.state.operator``. The GET handler reads it from there
    so the dependency-injection list stays short. Raises
    ``AttributeError`` only if the parent dependency chain has been
    skipped — which would itself be a wiring bug, not a route bug.
    """
    return cast(Operator, request.state.operator)


__all__ = ["router"]
