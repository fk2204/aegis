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
from aegis.api.deps import get_audit, get_funder_repository, get_ofac_client
from aegis.audit import AuditLog
from aegis.funders.repository import FunderRepository
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import DealMatchResult, ScoreInput, ScoreResult
from aegis.scoring.ofac import OFACClient, OFACStaleError
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
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> ScoreResult:
    try:
        result = score_deal(deal, ofac=ofac)
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
            "ofac_consulted": ofac is not None,
        },
    )
    return result


@router.post(
    "/score-with-matches",
    response_model=DealMatchResult,
    summary="Score a deal AND return matched funders ranked by match_score.",
)
def score_with_matches(
    deal: ScoreInput,
    audit: Annotated[AuditLog, Depends(get_audit)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
) -> DealMatchResult:
    """Phase 7B endpoint powering the dashboard's matched-funders panel.

    Runs scoring once, then iterates over active funders in the repository
    calling ``match_funder`` per row. Funders that the matcher returns
    ``None`` for (inactive or no criteria) are dropped. The remainder
    sort by ``match_score`` descending.
    """
    try:
        score_result = score_deal(deal, ofac=ofac)
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    matches = []
    for funder in funder_repo.list_active():
        match = match_funder(funder, deal, score_result)
        if match is not None:
            matches.append(match)
    matches.sort(key=lambda m: m.match_score, reverse=True)

    audit.record(
        actor="api",
        action="deal.score_with_matches",
        subject_type="merchant",
        subject_id=deal.merchant_id,
        details={
            "score": score_result.score,
            "tier": score_result.tier,
            "recommendation": score_result.recommendation,
            "matched_funder_count": len(matches),
            "ofac_consulted": ofac is not None,
        },
    )
    return DealMatchResult(score=score_result, matched_funders=matches)


__all__ = ["router"]
