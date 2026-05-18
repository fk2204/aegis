"""/deals — score a merchant + parsed-document pair.

POST /deals/score takes a ``ScoreInput`` and returns a ``ScoreResult``.
The route does not own the merge from merchant + analysis to
ScoreInput — that's ``aegis.scoring.build_score_input.build_score_input``,
which the operator can call directly when building from Supabase rows.
This route exists so the dashboard + Zoho sync have one canonical
``score_deal`` endpoint to hit, and so OFAC failures surface as 503
rather than swallowing into a quietly-allowed sanctioned merchant.

POST /deals/{merchant_id}/sync-to-zoho takes a ``ScoreResult`` and
upserts the merchant into Zoho's Leads (default) or Deals module,
selected by the ``target`` query parameter. Operator-triggered (no
auto-push from /score) so the rep reviews the score first.

Decision snapshot wiring (mp Phase 2): when ``document_id`` is supplied
as a query parameter, the score endpoints also write an immutable row
to the ``decisions`` table per master plan §9.2 — one snapshot per
approve / decline / manual_review call. The snapshot is what
regulators and counsel read six months later; the audit_log entry
sitting alongside is the cross-reference. ``document_id`` is optional
so existing API callers don't break, but every production caller
(dashboard, Zoho sync) is expected to pass it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

import aegis
from aegis.api.auth import require_bearer
from aegis.api.deps import (
    get_audit,
    get_decision_snapshot,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.api.routes.findings import build_merchant_findings
from aegis.audit import AuditLog, AuditWriteError
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
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import DealMatchResult, ScoreInput, ScoreResult
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
from aegis.storage import DocumentRepository
from aegis.web._findings_csv import findings_to_csv
from aegis.web._slug import slugify
from aegis.zoho.client import ZohoAuthError, ZohoClient, ZohoError
from aegis.zoho.sync import ZohoSync, ZohoSyncError

_log = get_logger(__name__)


router = APIRouter(prefix="/deals", tags=["deals"], dependencies=[Depends(require_bearer)])


_RECOMMENDATION_TO_DECISION: dict[str, DecisionLiteral] = {
    "approve": "approve",
    "decline": "decline",
    "refer": "manual_review",
}


class ZohoSyncResponse(BaseModel):
    """Result of pushing a scored merchant into Zoho's Leads or Deals module."""

    merchant_id: UUID
    target: Literal["lead", "deal"]
    zoho_record_id: str
    action: str  # "created" | "updated"


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


def _maybe_record_decision(
    *,
    document_id: UUID | None,
    deal: ScoreInput,
    result: ScoreResult,
    matrix: StateMatrix,
    snapshot: DecisionSnapshot,
    audit: AuditLog,
    decided_by: str = "api",
) -> None:
    """Write a decision snapshot when ``document_id`` is supplied.

    Absent document_id → no snapshot (the caller is a tooling path that
    doesn't yet bind to a statement). The route still returns the
    score; only the immutable evidence row is skipped. A warning logs
    so the gap is visible.
    """
    if document_id is None:
        _log.warning(
            "decision.snapshot_skipped reason=no_document_id merchant=%s recommendation=%s",
            deal.merchant_id,
            result.recommendation,
        )
        return
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
    document_id: UUID | None = None,
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
            # decline_details carries the matched_name + sdn_uid that fired
            # an OFAC hard decline — required for the 10-business-day
            # Initial Report of Blocked Property (docs/compliance/07_*).
            "decline_details": result.decline_details,
            "ofac_consulted": ofac is not None,
            "document_id": str(document_id) if document_id else None,
        },
    )
    _maybe_record_decision(
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
    document_id: UUID | None = None,
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
            "decline_details": score_result.decline_details,
            "ofac_consulted": ofac is not None,
            "document_id": str(document_id) if document_id else None,
        },
    )
    _maybe_record_decision(
        document_id=document_id,
        deal=deal,
        result=score_result,
        matrix=_state_matrix(request),
        snapshot=snapshot,
        audit=audit,
    )
    return DealMatchResult(score=score_result, matched_funders=matches)


@router.post(
    "/{merchant_id}/sync-to-zoho",
    response_model=ZohoSyncResponse,
    summary="Push merchant + score result to Zoho's Leads or Deals module.",
)
def sync_to_zoho(
    merchant_id: UUID,
    score_result: ScoreResult,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    target: Literal["lead", "deal"] = "lead",
    attach_findings: bool = True,
) -> ZohoSyncResponse:
    """Operator-triggered Zoho upsert into Leads or Deals.

    The ``target`` query parameter selects the destination module and
    defaults to ``"lead"`` — the natural early-pipeline target (website
    form → Lead → enriched-by-Aegis → rep converts → Deal). Pass
    ``target=deal`` to push directly into the Deals module instead.

    Idempotent on the matching merchant id field: ``merchant.zoho_lead_id``
    for leads, ``merchant.zoho_deal_id`` for deals. Absent ⇒ create new
    record and persist id back to merchant; present ⇒ update in place.
    Auth + audit are inherited from the deals router; rate limits +
    retries are handled by ``ZohoClient`` (tenacity backoff on 429/5xx).

    Surfaces ``ZohoAuthError`` as 503 (configuration problem, not the
    caller's fault) and ``ZohoSyncError`` as 502 (Zoho responded but
    response was unusable). ``ZohoError`` other than auth means Zoho
    returned a 4xx — re-raised as 502 with detail for operator triage.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"merchant {merchant_id} not found",
        ) from exc

    if target == "lead":
        was_create = merchant.zoho_lead_id is None
    else:
        was_create = merchant.zoho_deal_id is None

    try:
        with ZohoClient() as client:
            sync = ZohoSync(client=client, merchants=merchants, audit=audit)
            if target == "lead":
                zoho_record_id = sync.push_merchant_to_lead(merchant_id, score_result)
                module = "Leads"
            else:
                zoho_record_id = sync.push_merchant_with_score(merchant_id, score_result)
                module = "Deals"

            # U1: attach findings CSV so the rep sees it inside Zoho.
            # Attachment failure does NOT fail the request — upsert already
            # succeeded and is the load-bearing operation.
            if attach_findings:
                try:
                    findings = build_merchant_findings(
                        merchant=merchant, docs=docs, ofac=ofac
                    )
                    csv_bytes = findings_to_csv(findings).encode("utf-8")
                    filename = f"findings_{slugify(merchant.business_name)}.csv"
                    sync.attach_findings_csv(
                        module=module,
                        record_id=zoho_record_id,
                        merchant_id=merchant_id,
                        csv_bytes=csv_bytes,
                        filename=filename,
                    )
                except Exception as exc:  # broad on purpose — see comment above
                    _log.warning(
                        "findings csv attach skipped",
                        extra={
                            "merchant_id": str(merchant_id),
                            "error": str(exc),
                        },
                    )
    except ZohoAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"zoho_auth_unavailable: {exc}",
        ) from exc
    except ZohoSyncError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"zoho_sync_error: {exc}",
        ) from exc
    except ZohoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"zoho_error: {exc}",
        ) from exc

    return ZohoSyncResponse(
        merchant_id=merchant_id,
        target=target,
        zoho_record_id=zoho_record_id,
        action="created" if was_create else "updated",
    )


__all__ = ["router"]
