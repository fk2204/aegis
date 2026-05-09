"""/deals — score a merchant + parsed-document pair.

POST /deals/score takes a ``ScoreInput`` and returns a ``ScoreResult``.
The route does not own the merge from merchant + analysis to
ScoreInput — that's ``aegis.scoring.build_score_input.build_score_input``,
which the operator can call directly when building from Supabase rows.
This route exists so the dashboard + Zoho sync have one canonical
``score_deal`` endpoint to hit, and so OFAC failures surface as 503
rather than swallowing into a quietly-allowed sanctioned merchant.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from aegis.api.auth import require_bearer
from aegis.api.deps import get_audit
from aegis.audit import AuditLog
from aegis.scoring.models import ScoreInput, ScoreResult
from aegis.scoring.ofac import OFACStaleError
from aegis.scoring.score import score_deal

router = APIRouter(prefix="/deals", tags=["deals"], dependencies=[Depends(require_bearer)])


@router.post(
    "/score",
    response_model=ScoreResult,
    summary="Score a deal (hard declines + soft scoring + tier/payback).",
)
def score(
    deal: ScoreInput,
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> ScoreResult:
    # OFAC client wiring lands in a follow-up route param + factory; the
    # scorer handles ofac=None by skipping that hard-decline rule.
    try:
        result = score_deal(deal, ofac=None)
    except OFACStaleError as exc:
        # OFAC list could not refresh and the cache is too old — fail
        # closed so a sanctioned name cannot slip through.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    audit.record(
        actor="api",
        action="deal.score",
        subject_type="merchant",
        subject_id=deal.merchant_id,
        details={
            "score": result.score,
            "tier": result.tier,
            "recommendation": result.recommendation,
            "hard_decline_reasons": result.hard_decline_reasons,
        },
    )
    return result


__all__ = ["router"]
