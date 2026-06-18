"""/deals — score a merchant + parsed-document pair.

POST /deals/score takes a ``ScoreInput`` and returns a ``ScoreResult``.
The route does not own the merge from merchant + analysis to
ScoreInput — that's ``aegis.scoring.build_score_input.build_score_input``,
which the operator can call directly when building from Supabase rows.
This route exists so the dashboard + Close sync have one canonical
``score_deal`` endpoint to hit, and so OFAC failures surface as 503
rather than swallowing into a quietly-allowed sanctioned merchant.

POST /deals/{merchant_id}/sync-to-close pushes the latest stored
decision for a merchant onto their Close Lead's Aegis-* custom fields.
Operator-triggered (no auto-push from /score) so the operator reviews
the score first. The merchant must already be linked to a Close Lead
(``merchants.close_lead_id`` populated by the /webhooks/close inbound
handler in step 4). Idempotency lives in
``aegis.close.sync.push_decision_to_close`` — see step 5.

Decision snapshot wiring (mp Phase 2 / U17): ``document_id`` is REQUIRED
on every score-emitting route. Each call writes an immutable row to the
``decisions`` table per master plan §9.2 — one snapshot per approve /
decline / manual_review call. The snapshot is what regulators and
counsel read six months later; the audit_log entry sitting alongside
is the cross-reference. Calls that omit ``document_id`` now 422 (FastAPI
validation) rather than silently producing a score with no snapshot —
the U13 portfolio audit-log fallback existed to paper over historical
gaps; U17 removes the gap at its source.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

import aegis
from aegis.api.auth import require_bearer
from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_decision_snapshot,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.audit import AuditLog, AuditWriteError
from aegis.close.client import CloseAuthError, CloseClient, CloseError
from aegis.close.sync import (
    SyncError,
    derive_ofac_status,
    push_decision_to_close,
    push_offer_to_opportunity,
)
from aegis.compliance.router import router as compliance_router
from aegis.compliance.snapshot import (
    DecisionLiteral,
    DecisionPayload,
    DecisionSnapshot,
    DecisionSnapshotError,
    record_decision,
)
from aegis.compliance.state_matrix import StateMatrix
from aegis.funders.repository import FunderRepository
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.ops.operators import resolve_operator_email
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import DealMatchResult, ScoreInput, ScoreResult
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
from aegis.scoring_v2.score_deal_inputs import compute_score_deal_track_inputs
from aegis.scoring_v2.track_a import IntegrityVerdict
from aegis.scoring_v2.track_b import BusinessRiskBand
from aegis.storage import DocumentRepository

_log = get_logger(__name__)


router = APIRouter(prefix="/deals", tags=["deals"], dependencies=[Depends(require_bearer)])


_RECOMMENDATION_TO_DECISION: dict[str, DecisionLiteral] = {
    "approve": "approve",
    "decline": "decline",
    "refer": "manual_review",
}


class CloseSyncResponse(BaseModel):
    """Result of pushing the latest stored decision to a Close Lead.

    Mirrors ``aegis.close.sync.SyncResult`` plus the merchant + Lead +
    decision identifiers the operator needs to reconcile.
    """

    merchant_id: UUID
    close_lead_id: str
    decision_id: UUID
    patched: bool
    fields_diffed: list[str]
    reason: Literal["patched", "no_diff", "lead_not_found"]


def _track_inputs_for_deal(
    docs: DocumentRepository,
    merchant_id: UUID,
) -> tuple[IntegrityVerdict | None, BusinessRiskBand | None]:
    """Look up the merchant's documents + analyses and compute the
    Track A integrity verdict + Track B risk band that ``score_deal``
    consumes when ``AEGIS_SCORING_ENGINE=track_abc``.

    Returns ``(None, None)`` on any failure (missing merchant, no docs,
    parse error) so the legacy engine remains the safe fallback —
    matches the discipline in
    ``aegis.scoring_v2.score_deal_inputs.compute_score_deal_track_inputs``.
    """
    try:
        all_docs = docs.list_documents(merchant_id=merchant_id, limit=50)
        if not all_docs:
            return None, None
        analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in all_docs])
    except Exception as exc:
        _log.warning(
            "deals.track_inputs_lookup_failed merchant_id=%s err=%s",
            merchant_id,
            exc.__class__.__name__,
        )
        return None, None
    return compute_score_deal_track_inputs(
        documents=all_docs,
        list_transactions=docs.list_transactions,
        analyses_by_doc=analyses_by_doc,
        merchant_id=merchant_id,
    )


def _state_matrix(request: Request) -> StateMatrix:
    """Pull the boot-loaded state matrix off ``app.state``.

    The matrix is set in ``aegis.api.app._lifespan``; reading it via
    request avoids re-loading from disk on every scoring call.
    """
    matrix: StateMatrix | None = getattr(request.app.state, "state_matrix", None)
    if matrix is None:  # pragma: no cover — lifespan should always set it
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="state_matrix_not_loaded",
        )
    return matrix


def _build_decision_payload(
    *,
    deal_id: UUID,
    deal: ScoreInput,
    result: ScoreResult,
    matrix: StateMatrix,
    decided_by: str,
) -> DecisionPayload:
    """Translate a scored deal into a DecisionPayload.

    Pulls cfdl_tier + applicable rules from the compliance router so the
    snapshot pins the regulatory surface that applied at decision time.
    score_factors carries the soft-scoring breakdown — what the auditor
    needs to reconstruct *why* the score was what it was.
    """
    route = compliance_router(
        state_code=deal.state,
        deal_amount=deal.requested_amount,
        product_type="sales_based",  # MCA is the only product AEGIS scores today
        matrix=matrix,
    )
    apr_calculated: Decimal | None = None
    if result.apr is not None:
        # ``DecisionPayload.apr_calculated`` is bounded numeric(8,4).
        apr_calculated = result.apr
    return DecisionPayload(
        deal_id=deal_id,
        decided_by=decided_by,
        decision=_RECOMMENDATION_TO_DECISION[result.recommendation],
        decision_reason_codes=list(result.hard_decline_reasons),
        score=Decimal(result.score),
        score_factors={
            "tier": result.tier,
            "breakdown": result.breakdown,
            "soft_concerns": list(result.soft_concerns),
        },
        state_code=deal.state.upper(),
        cfdl_tier=route.tier,
        apr_calculated=apr_calculated,
        apr_method="reg_z_1026_22" if apr_calculated is not None else None,
        aegis_version=aegis.__version__,
        rule_pack_version=matrix.version,
    )


def _record_decision(
    *,
    document_id: UUID,
    deal: ScoreInput,
    result: ScoreResult,
    matrix: StateMatrix,
    snapshot: DecisionSnapshot,
    audit: AuditLog,
    decided_by: str = "api",
) -> None:
    """Write an immutable decision snapshot for the scored deal.

    ``document_id`` is required at the route layer (U17), so this helper
    no longer has a "skip if absent" branch. Per master plan §2
    principle 3, a decision without a snapshot is a regulator-defense
    gap — failures here surface as 503 so the caller can retry rather
    than silently returning a score with no audit trail.
    """
    payload = _build_decision_payload(
        deal_id=document_id,
        deal=deal,
        result=result,
        matrix=matrix,
        decided_by=decided_by,
    )
    try:
        record_decision(payload, snapshot=snapshot, audit=audit)
    except (DecisionSnapshotError, AuditWriteError) as exc:
        # Per master plan §2 principle 3: a decision without a snapshot
        # is a regulator-defense gap. Fail the request rather than
        # silently return the score; the caller can retry.
        _log.error(
            "decision.snapshot_failed deal_id=%s decision=%s err=%s",
            document_id,
            result.recommendation,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"decision_snapshot_unavailable: {exc}",
        ) from exc


@router.post(
    "/score",
    response_model=ScoreResult,
    summary="Score a deal (hard declines + soft scoring + tier/payback).",
)
def score(
    deal: ScoreInput,
    request: Request,
    audit: Annotated[AuditLog, Depends(get_audit)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    document_id: Annotated[
        UUID,
        Query(
            description=(
                "document_id is required so every scoring decision "
                "produces an immutable snapshot row in the decisions "
                "table. Calls that omit it 422 (U17, breaking change "
                "from the pre-U17 optional behavior)."
            ),
        ),
    ],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> ScoreResult:
    """Score a deal. ``document_id`` is required so every scoring
    decision produces an immutable snapshot row in the decisions table.

    Breaking change (U17): pre-U17 callers that omitted ``document_id``
    received a score with no snapshot; the call now 422s instead. Every
    production caller (dashboard, Close sync) already supplies it.
    """
    # Web-presence reputation scan (migration 067). The scan is a soft
    # signal — risk_flags surface as ``FunderMatch.soft_concerns`` via
    # ``match_funder``. Run lazily on the first score where the merchant
    # has no prior scan; subsequent scores reuse the persisted result so
    # the Bedrock + web_search round-trip cost is paid once per merchant
    # (refresh via the dossier button to re-scan on demand). Bedrock
    # failures persist an empty result so a transient outage doesn't
    # re-fire on every subsequent score.
    try:
        merchant_row = merchants.get(deal.merchant_id)
    except MerchantNotFoundError:
        merchant_row = None
    if merchant_row is not None:
        from aegis.web_presence.refresh import ensure_web_presence_scan

        ensure_web_presence_scan(
            merchant_row,
            merchants_repo=merchants,
            audit=audit,
        )

    # U33 — feed Track A/B verdicts. The route operates on an operator-
    # provided ScoreInput payload but the merchant's docs are available
    # via ``deal.merchant_id`` so the active-engine flip has live inputs.
    track_a_verdict, track_b_band = _track_inputs_for_deal(docs, deal.merchant_id)
    try:
        result = score_deal(
            deal,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except OFACStaleError as exc:
        # OFAC list could not refresh and the cache is too old — fail
        # closed so a sanctioned name cannot slip through.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    audit.record(
        actor="api",
        actor_email=actor_email,
        action="deal.score",
        subject_type="merchant",
        subject_id=deal.merchant_id,
        details={
            "score": result.score,
            "tier": result.tier,
            "recommendation": result.recommendation,
            "hard_decline_reasons": result.hard_decline_reasons,
            # decline_details carries the matched_name + sdn_uid that fired
            # an OFAC hard decline — required for the 10-business-day
            # Initial Report of Blocked Property (docs/compliance/07_*).
            "decline_details": result.decline_details,
            "ofac_consulted": ofac is not None,
            "document_id": str(document_id),
        },
    )
    _record_decision(
        document_id=document_id,
        deal=deal,
        result=result,
        matrix=_state_matrix(request),
        snapshot=snapshot,
        audit=audit,
    )
    return result


@router.post(
    "/score-with-matches",
    response_model=DealMatchResult,
    summary="Score a deal AND return matched funders ranked by match_score.",
)
def score_with_matches(
    deal: ScoreInput,
    request: Request,
    audit: Annotated[AuditLog, Depends(get_audit)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    document_id: Annotated[
        UUID,
        Query(
            description=(
                "document_id is required so every scoring decision "
                "produces an immutable snapshot row in the decisions "
                "table. Calls that omit it 422 (U17, breaking change "
                "from the pre-U17 optional behavior)."
            ),
        ),
    ],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> DealMatchResult:
    """Phase 7B endpoint powering the dashboard's matched-funders panel.

    Runs scoring once, then iterates over active funders in the repository
    calling ``match_funder`` per row. Funders that the matcher returns
    ``None`` for (inactive or no criteria) are dropped. The remainder
    sort by ``match_score`` descending.

    ``document_id`` is required so every scoring decision produces an
    immutable snapshot row in the decisions table. Breaking change
    (U17): pre-U17 callers that omitted ``document_id`` received a score
    with no snapshot; the call now 422s instead.
    """
    # U33 — feed Track A/B verdicts; same pattern as ``/score``.
    track_a_verdict, track_b_band = _track_inputs_for_deal(docs, deal.merchant_id)
    try:
        score_result = score_deal(
            deal,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
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
        actor_email=actor_email,
        action="deal.score_with_matches",
        subject_type="merchant",
        subject_id=deal.merchant_id,
        details={
            "score": score_result.score,
            "tier": score_result.tier,
            "recommendation": score_result.recommendation,
            "matched_funder_count": len(matches),
            "decline_details": score_result.decline_details,
            "ofac_consulted": ofac is not None,
            "document_id": str(document_id),
        },
    )
    _record_decision(
        document_id=document_id,
        deal=deal,
        result=score_result,
        matrix=_state_matrix(request),
        snapshot=snapshot,
        audit=audit,
    )
    return DealMatchResult(score=score_result, matched_funders=matches)


@router.post(
    "/{merchant_id}/sync-to-close",
    response_model=CloseSyncResponse,
    summary="Push the latest stored decision for a merchant to its Close Lead.",
)
def sync_to_close(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> CloseSyncResponse:
    """Operator-triggered write-back of a merchant's latest decision to
    Close.

    Pipeline:

    1. Load merchant. ``404`` if no row matches ``merchant_id``.
    2. ``400`` if ``merchant.close_lead_id`` is null — the Lead hasn't
       been linked via the inbound webhook yet (operator hasn't moved
       the Close Opportunity to "Docs In — Pre-UW").
    3. Look up the latest decision for this merchant via documents
       belonging to it. ``400`` if no decision exists yet — nothing to
       push.
    4. Audit ``close.deal.sync_triggered`` once we have a real
       trigger event (merchant + decision + operator email). The
       downstream ``push_decision_to_close`` writes its own
       ``close.lead.sync_attempted`` row — don't duplicate.
    5. Derive OFAC status from the stored decision and call
       ``push_decision_to_close``. The function PATCHes only when a
       business field actually changed (idempotency guarantee #4).
    6. Return ``CloseSyncResponse`` mirroring ``SyncResult`` so the
       caller knows whether a PATCH fired and what changed.

    Error map:
      * ``CloseAuthError`` (401 from Close, or missing API key) → 503.
      * Any other ``CloseError`` (5xx after retries, 4xx Close didn't
        accept) → 502. The CloseClient already handles 401 fail-fast
        and 429/5xx retries.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"merchant {merchant_id} not found",
        ) from exc

    if not merchant.close_lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"merchant {merchant_id} has no close_lead_id; "
                "Lead must be linked via /webhooks/close first (operator "
                "moves Opportunity to 'Docs In — Pre-UW' in Close)"
            ),
        )

    deal_ids = [doc.id for doc in docs.list_documents(merchant_id=merchant_id)]
    decision = snapshot.find_latest_for_merchant(merchant_id, deal_ids=deal_ids)
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"merchant {merchant_id} has no recorded decision yet; "
                "score the deal first via /deals/score before syncing"
            ),
        )

    # Audit the trigger BEFORE pushing — captures who tried what,
    # regardless of whether the downstream PATCH actually fires.
    audit.record(
        actor="api",
        action="close.deal.sync_triggered",
        subject_type="merchant",
        subject_id=merchant_id,
        actor_email=actor_email,
        details={
            "merchant_id": str(merchant_id),
            "decision_id": str(decision.id),
            "close_lead_id": merchant.close_lead_id,
        },
    )

    ofac_status = derive_ofac_status(
        decision_reason_codes=list(decision.decision_reason_codes),
        ofac_cache_timestamp=decision.ofac_cache_timestamp,
    )

    try:
        result = push_decision_to_close(
            close_lead_id=merchant.close_lead_id,
            decision_id=decision.id,
            score=decision.score,
            recommendation=decision.decision,
            ofac_status=ofac_status,
            client=close_client,
            audit=audit,
            merchant=merchant,
        )
    except CloseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"close_auth_unavailable: {exc}",
        ) from exc
    except CloseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"close_upstream_error: {exc}",
        ) from exc
    except SyncError as exc:
        # Recommendation literal that this route can't push (e.g.
        # "redisclosure"). Surface as 400 — the caller asked for
        # something we can't fulfill, not an upstream issue.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"close_sync_unsupported_decision: {exc}",
        ) from exc

    # Stage 7 — offer + supporting cashflow snapshot → the linked Close
    # Opportunity. Best-effort: a missing ``close_opportunity_id`` (legacy
    # merchant pre-migration 054, or webhook hasn't fired yet for this
    # merchant) silently skips the opportunity-side push. A failure on
    # this push does NOT roll back the Lead-side PATCH that already
    # landed above — the two sides are independent in Close, the audit
    # rows say what happened on each, and the dossier still renders.
    if merchant.close_opportunity_id is not None:
        _try_push_offer_to_opportunity(
            merchant=merchant,
            merchant_id=merchant_id,
            close_opportunity_id=merchant.close_opportunity_id,
            decision_id=decision.id,
            docs=docs,
            close_client=close_client,
            audit=audit,
            ofac=ofac,
        )

    return CloseSyncResponse(
        merchant_id=merchant_id,
        close_lead_id=merchant.close_lead_id,
        decision_id=decision.id,
        patched=result.patched,
        fields_diffed=result.fields_diffed,
        reason=result.reason,
    )


def _try_push_offer_to_opportunity(
    *,
    merchant: MerchantRow,
    merchant_id: UUID,
    close_opportunity_id: str,
    decision_id: UUID,
    docs: DocumentRepository,
    close_client: CloseClient,
    audit: AuditLog,
    ofac: OFACClient | None,
) -> None:
    """Compute the offer + supporting fields and PATCH them onto the
    Close Opportunity. Best-effort: any compute-side failure logs and
    returns silently rather than raising — the Lead-side PATCH already
    landed and the dossier is unchanged.

    Sources the seven push fields from:

      * ``analysis.monthly_revenue``      -> True Revenue
      * Operator-entered ``Holdback Capacity`` on the opportunity,
        with ``analysis.monthly_revenue * 0.25`` as the fallback when
        Close hasn't been populated yet. Read once via the
        ``opportunity_payload`` GET that's already needed for the
        push's read-before-write diff — no second round-trip.
      * ``mca_stack.active_mca_count``    -> Existing MCA Debits Identified
      * ``analysis.mca_daily_total``      -> Existing MCA Daily Debits Total
      * ``offer.recommended_amount``      -> Suggested Max Advance
      * ``offer.holdback_pct``            -> Recommended Holdback Pct
      * ``ScoreResult.recommended_factor_rate`` via
        ``recommended_factor_rate_from`` -> Recommended Factor Rate.
        ``None`` when scorer didn't produce a meaningful recommendation
        (merchant unscored, hard decline, sub-1.0 factor).

    ``holdback_capacity_monthly`` is NOT pushed back to Close: the
    operator owns that field. AEGIS reads it for sizing but treats it
    as operator-write-only.

    Decision-boundary posture: SHADOW ONLY. None of these fields drive
    the AEGIS live decline path.
    """
    # Avoid circular imports for the scoring_v2 modules — this helper
    # only runs on the operator-triggered sync route, not the dossier
    # render hot path.
    from aegis.close.field_map import CLOSE_OPPORTUNITY_FIELD_IDS, parse_money
    from aegis.scoring_v2.mca_stack import aggregate_mca_stack
    from aegis.scoring_v2.offer import compute_offer
    from aegis.scoring_v2.score_for_sync import (
        compute_score_result_for_default_bundle,
        recommended_factor_rate_from,
    )

    documents_list = docs.list_documents(merchant_id=merchant_id)
    if not documents_list:
        return
    latest_doc = max(documents_list, key=lambda d: d.uploaded_at)
    analysis = docs.get_analysis(latest_doc.id)
    if analysis is None:
        return
    transactions = docs.list_transactions(latest_doc.id)

    # GET the opportunity once. Used to (a) read the operator-entered
    # ``Holdback Capacity`` value and (b) feed ``push_offer_to_opportunity``'s
    # read-before-write diff without a second round-trip.
    try:
        opportunity_payload = close_client.get_opportunity(close_opportunity_id)
    except CloseError as exc:
        if exc.status_code == 404:
            audit.record(
                actor="close_sync",
                action="close.opportunity.sync_failed_not_found",
                details={
                    "close_opportunity_id": close_opportunity_id,
                    "decision_id": str(decision_id),
                    "status_code": 404,
                },
            )
            return
        # Other CloseError: best-effort — Lead-side PATCH already landed.
        return

    operator_holdback_raw = opportunity_payload.get(
        f"custom.{CLOSE_OPPORTUNITY_FIELD_IDS['holdback_capacity']}"
    )
    try:
        operator_holdback = parse_money(operator_holdback_raw)
    except Exception:
        operator_holdback = None
    holdback_capacity = (
        operator_holdback
        if operator_holdback is not None and operator_holdback > 0
        else analysis.monthly_revenue * Decimal("0.25")
    )

    mca_stack = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=analysis.monthly_revenue,
        period_days=analysis.statement_days,
    )
    offer = compute_offer(
        true_revenue_monthly=analysis.monthly_revenue,
        holdback_capacity_monthly=holdback_capacity,
        mca_stack=mca_stack,
    )

    score_result = compute_score_result_for_default_bundle(
        merchant=merchant,
        merchant_id=merchant_id,
        docs=docs,
        ofac=ofac,
    )

    try:
        push_offer_to_opportunity(
            close_opportunity_id=close_opportunity_id,
            decision_id=decision_id,
            suggested_max_advance=(offer.recommended_amount if offer is not None else None),
            recommended_factor_rate=recommended_factor_rate_from(score_result),
            recommended_holdback_pct=(offer.holdback_pct if offer is not None else None),
            true_revenue_monthly=analysis.monthly_revenue,
            # Operator owns ``Holdback Capacity``. We READ it above to
            # feed the offer math; we do NOT push back — passing None
            # makes ``push_offer_to_opportunity`` skip the field entirely.
            holdback_capacity_monthly=None,
            existing_mca_count=mca_stack.active_mca_count,
            existing_mca_daily_total=analysis.mca_daily_total,
            client=close_client,
            audit=audit,
            opportunity_payload=opportunity_payload,
        )
    except (CloseError, CloseAuthError):
        # Audit rows already captured by ``push_offer_to_opportunity``;
        # Lead-side PATCH from earlier in this request already landed.
        # Don't 5xx the whole sync — the operator can re-trigger
        # /sync-to-close cheaply (idempotent).
        return


__all__ = ["router"]
