"""GET /merchants/{merchant_id}/findings — structured underwriting export.

Heron-shaped findings payload that downstream tooling can consume:
merchant header, intake context, per-document parser results, latest
score breakdown, stacking summary, and a compliance ribbon. EIN is
NEVER included (excluded by ``model_dump`` projection). The field is
masked in logs at ingest time.

Response is the canonical shape; the dashboard CSV download flattens
the same payload for Excel.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from aegis.api.auth import require_bearer
from aegis.api.deps import (
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.merchants.models import EntityType, IndustryRiskTier, MerchantRow
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository
from aegis.money import Money
from aegis.scoring.models import ScoreResult
from aegis.scoring.ofac import OFACClient
from aegis.storage import AnalysisRow, DocumentRepository, DocumentRow
from aegis.web._stacking_card import StackingCard, build_stacking_card

GENERATOR_VERSION = "findings-v1"


router = APIRouter(
    prefix="/merchants",
    tags=["findings"],
    dependencies=[Depends(require_bearer)],
)


class MerchantHeader(BaseModel):
    """Merchant identity + intake fields. EIN excluded."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    business_name: str
    dba: str | None
    owner_name: str
    state: str
    industry_naics: str | None
    industry_risk_tier: IndustryRiskTier | None
    entity_type: EntityType | None
    time_in_business_months: int | None
    credit_score: int | None
    requested_amount: Money | None
    requested_factor: Decimal | None
    requested_term_days: int | None
    broker_source: str | None
    intake_date: date | None
    is_renewal: bool


class DocumentFindings(BaseModel):
    """One parsed document, flattened for export."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    parse_status: str
    fraud_score: int | None
    uploaded_at: datetime
    statement_period_start: date | None
    statement_period_end: date | None
    statement_days: int | None
    true_revenue: Money | None
    avg_daily_balance: Money | None
    lowest_balance: Money | None
    num_nsf: int | None
    days_negative: int | None
    mca_positions: int | None
    mca_daily_total: Money | None
    debt_to_revenue: Decimal | None
    payroll_detected: bool | None
    flags: list[str]


class StackingSummary(BaseModel):
    """Stacking display payload mirrored to the JSON shape."""

    model_config = ConfigDict(extra="forbid")

    daily_total: Decimal
    monthly_burden: Decimal
    position_count: int
    debit_count: int


class ComplianceRibbon(BaseModel):
    """OFAC + state tier + renewal at a glance."""

    model_config = ConfigDict(extra="forbid")

    state_tier: int | Literal["unserved"]
    ofac_status: Literal["checked", "stale", "unavailable", "not_consulted"]
    ofac_match: bool | None
    is_renewal: bool


class MerchantFindings(BaseModel):
    """Complete findings export for one merchant."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    generator_version: str
    merchant: MerchantHeader
    documents: list[DocumentFindings]
    latest_score: ScoreResult | None
    stacking: StackingSummary | None
    compliance: ComplianceRibbon


@router.get(
    "/{merchant_id}/findings",
    response_model=MerchantFindings,
    summary="Structured underwriting findings (JSON). EIN is excluded.",
)
def get_findings(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> MerchantFindings:
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    return build_merchant_findings(merchant=merchant, docs=docs, ofac=ofac)


# Shared builder — used by JSON route + dashboard CSV route ------------------


def build_merchant_findings(
    *,
    merchant: MerchantRow,
    docs: DocumentRepository,
    ofac: OFACClient | None,
    score_result: ScoreResult | None = None,
    stacking_card: StackingCard | None = None,
) -> MerchantFindings:
    """Compose the findings payload.

    ``score_result`` and ``stacking_card`` may be passed precomputed by
    the dashboard route (which already runs them for the panel) so we
    avoid double-scoring on the CSV download path. When omitted the
    builder runs them itself for the JSON API path.
    """
    from aegis.compliance.states import STATES
    from aegis.scoring.ofac import OFACStaleError
    from aegis.scoring.score import score_deal

    all_docs = docs.list_documents(merchant_id=merchant.id, limit=50)
    doc_findings = [
        _document_findings(doc, docs.get_analysis(doc.id)) for doc in all_docs
    ]

    latest_doc = all_docs[0] if all_docs else None
    latest_analysis = docs.get_analysis(latest_doc.id) if latest_doc else None

    if score_result is None and latest_doc is not None and latest_analysis is not None:
        from aegis.web.router import _score_input_from_dashboard

        try:
            score_result = score_deal(
                _score_input_from_dashboard(merchant, latest_doc, latest_analysis),
                ofac=ofac,
            )
        except OFACStaleError:
            score_result = None

    if stacking_card is None and latest_doc is not None and latest_analysis is not None:
        stacking_card = build_stacking_card(
            latest_analysis, docs.list_transactions(latest_doc.id)
        )

    stacking_summary: StackingSummary | None = None
    if stacking_card is not None:
        stacking_summary = StackingSummary(
            daily_total=Decimal(stacking_card.daily_total),
            monthly_burden=Decimal(stacking_card.monthly_burden),
            position_count=stacking_card.position_count,
            debit_count=stacking_card.debit_count,
        )

    state_tier_val: int | Literal["unserved"]
    reg = STATES.get(merchant.state.upper())
    state_tier_val = "unserved" if reg is None else int(reg.tier)

    ofac_status: Literal["checked", "stale", "unavailable", "not_consulted"]
    ofac_match: bool | None = None
    if ofac is None:
        ofac_status = "not_consulted"
    else:
        try:
            ofac_match = ofac.is_match(merchant.business_name)
            ofac_status = "checked"
        except OFACStaleError:
            ofac_status = "stale"
        except Exception:
            ofac_status = "unavailable"

    return MerchantFindings(
        generated_at=datetime.now(UTC),
        generator_version=GENERATOR_VERSION,
        merchant=MerchantHeader(
            id=merchant.id,
            business_name=merchant.business_name,
            dba=merchant.dba,
            owner_name=merchant.owner_name,
            state=merchant.state,
            industry_naics=merchant.industry_naics,
            industry_risk_tier=merchant.industry_risk_tier,
            entity_type=merchant.entity_type,
            time_in_business_months=merchant.time_in_business_months,
            credit_score=merchant.credit_score,
            requested_amount=merchant.requested_amount,
            requested_factor=merchant.requested_factor,
            requested_term_days=merchant.requested_term_days,
            broker_source=merchant.broker_source,
            intake_date=merchant.intake_date,
            is_renewal=merchant.is_renewal,
        ),
        documents=doc_findings,
        latest_score=score_result,
        stacking=stacking_summary,
        compliance=ComplianceRibbon(
            state_tier=state_tier_val,
            ofac_status=ofac_status,
            ofac_match=ofac_match,
            is_renewal=merchant.is_renewal,
        ),
    )


def _document_findings(
    doc: DocumentRow, analysis: AnalysisRow | None
) -> DocumentFindings:
    if analysis is None:
        return DocumentFindings(
            document_id=doc.id,
            parse_status=doc.parse_status,
            fraud_score=doc.fraud_score,
            uploaded_at=doc.uploaded_at,
            statement_period_start=None,
            statement_period_end=None,
            statement_days=None,
            true_revenue=None,
            avg_daily_balance=None,
            lowest_balance=None,
            num_nsf=None,
            days_negative=None,
            mca_positions=None,
            mca_daily_total=None,
            debt_to_revenue=None,
            payroll_detected=None,
            flags=list(doc.all_flags),
        )
    return DocumentFindings(
        document_id=doc.id,
        parse_status=doc.parse_status,
        fraud_score=doc.fraud_score,
        uploaded_at=doc.uploaded_at,
        statement_period_start=analysis.statement_period_start,
        statement_period_end=analysis.statement_period_end,
        statement_days=analysis.statement_days,
        true_revenue=analysis.true_revenue,
        avg_daily_balance=analysis.avg_daily_balance,
        lowest_balance=analysis.lowest_balance,
        num_nsf=analysis.num_nsf,
        days_negative=analysis.days_negative,
        mca_positions=analysis.mca_positions,
        mca_daily_total=analysis.mca_daily_total,
        debt_to_revenue=analysis.debt_to_revenue,
        payroll_detected=analysis.payroll_detected,
        flags=list(doc.all_flags),
    )


__all__ = [
    "GENERATOR_VERSION",
    "ComplianceRibbon",
    "DocumentFindings",
    "MerchantFindings",
    "MerchantHeader",
    "StackingSummary",
    "build_merchant_findings",
    "router",
]
