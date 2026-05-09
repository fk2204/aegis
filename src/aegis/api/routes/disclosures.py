"""/disclosures — render the state-prescribed disclosure for a deal.

Routes a deal's state to the disclosure renderer:

  * Tier 1 → renders the regulator's prescribed Jinja template.
  * Tier 2 → renders the generic acknowledgment citing general law.
  * Tier 3 (unaudited) → 503 ``state_not_audited``. The operator must
    complete the audit before AEGIS can disclose for this state.
  * Non-served state → 422 ``state_not_served``.

Returns ``Content-Type: text/html`` so the operator can preview directly,
plus a JSON wrapper endpoint for programmatic callers (Zoho sync,
dashboard).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from aegis.api.auth import require_bearer
from aegis.compliance.disclosure import RenderedDisclosure, render_disclosure
from aegis.compliance.states import StateNotAudited, StateNotServed
from aegis.scoring.models import ScoreInput, ScoreResult

router = APIRouter(
    prefix="/disclosures",
    tags=["disclosures"],
    dependencies=[Depends(require_bearer)],
)


class DisclosureRequest(BaseModel):
    """Body for /disclosures/render — the deal + score the disclosure cites."""

    model_config = ConfigDict(extra="forbid")

    state: str
    deal: ScoreInput
    score: ScoreResult


def _render_or_raise(req: DisclosureRequest) -> RenderedDisclosure:
    try:
        return render_disclosure(req.state, req.deal, req.score)
    except StateNotServed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
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
    response_model=RenderedDisclosure,
    summary="Render a disclosure as JSON (state, tier, html, citation).",
)
def render_disclosure_json(req: DisclosureRequest) -> RenderedDisclosure:
    return _render_or_raise(req)


@router.post(
    "/render.html",
    response_class=HTMLResponse,
    summary="Render a disclosure and return raw HTML (text/html).",
)
def render_disclosure_html(req: DisclosureRequest) -> HTMLResponse:
    rendered = _render_or_raise(req)
    return HTMLResponse(content=rendered.html, status_code=200)


__all__ = ["DisclosureRequest", "router"]
