"""GET /audit/deal/{deal_id} — full evidence trail per deal (mp Phase 2).

Master plan §12: "returns all decisions chronologically, all disclosures
with delivery proof, all audit_log events, all linked statements/analyses."

JSON-first surface; the HTMX dashboard view can render from the same
payload in a later pass. Read-only — no writes happen here.

Authn: bearer token (consistent with the rest of the API). RLS on the
underlying tables denies anon role; service_role bypasses, which is what
the supabase-py client carries.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from aegis.api.auth import require_bearer
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(require_bearer)],
)


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class _ReadModel(BaseModel):
    """Permissive (extra=ignore) for forward compatibility — new columns
    can land in the underlying tables without breaking this endpoint."""

    model_config = ConfigDict(extra="ignore")


class DecisionRow(_ReadModel):
    id: UUID
    deal_id: UUID
    decided_at: datetime
    decided_by: str
    decision: str
    decision_reason_codes: list[str]
    score: Decimal | None = None
    score_factors: dict[str, Any] = {}
    analysis_id: UUID | None = None
    contributing_transaction_uuids: list[UUID] = []
    bank_statement_pdf_sha256: str | None = None
    state_code: str
    cfdl_tier: int
    disclosure_template_path: str | None = None
    disclosure_template_sha256: str | None = None
    disclosure_pdf_sha256: str | None = None
    apr_calculated: Decimal | None = None
    apr_method: str | None = None
    ofac_cache_timestamp: datetime | None = None
    ofac_cache_sha256: str | None = None
    aegis_version: str
    rule_pack_version: str
    backfill_quality: str | None = None


class DisclosureRow(_ReadModel):
    id: UUID
    deal_id: UUID
    decision_id: UUID | None = None
    state_code: str
    template_path: str | None = None
    template_sha256: str | None = None
    disclosure_type: str | None = None
    rendered_pdf_path: str | None = None
    rendered_pdf_sha256: str | None = None
    delivered_at: datetime | None = None
    delivery_method: str | None = None
    merchant_signature_at: datetime | None = None
    merchant_signature_ip: str | None = None
    merchant_signature_hash: str | None = None
    created_at: datetime | None = None


class AuditEvent(_ReadModel):
    actor: str
    action: str
    subject_type: str | None = None
    subject_id: UUID | None = None
    details: dict[str, Any] = {}
    created_at: datetime | None = None


class AnalysisStub(_ReadModel):
    """Minimal analysis shape — full aggregates are on the findings route."""

    id: UUID
    document_id: UUID
    statement_period_start: str | None = None
    statement_period_end: str | None = None
    created_at: datetime | None = None


class DealAuditView(BaseModel):
    """The full evidence trail for one deal.

    Fields are independently nullable / empty: a deal that hasn't reached
    a decision returns ``decisions=[]`` rather than 404. A 404 fires only
    when the underlying document doesn't exist.
    """

    model_config = ConfigDict(extra="forbid")

    deal_id: UUID
    decisions: list[DecisionRow]
    disclosures: list[DisclosureRow]
    audit_log: list[AuditEvent]
    analyses: list[AnalysisStub]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/deal/{deal_id}",
    response_model=DealAuditView,
    summary="Full audit trail for a deal: decisions + disclosures + audit_log + analyses.",
)
def get_deal_audit(deal_id: UUID) -> DealAuditView:
    client = get_supabase()

    # 404 if the document (deal) doesn't exist. Cheaper than running the
    # other queries against a missing FK.
    try:
        doc = (
            client.table("documents")
            .select("id")
            .eq("id", str(deal_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:
        _log.exception("audit.deal_lookup_failed deal_id=%s", deal_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit_db_unavailable",
        ) from exc

    if not (doc.data or []):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deal_not_found")

    decisions = _query_table_for_deal(client, "decisions", deal_id, order="decided_at")
    disclosures = _query_table_for_deal(client, "disclosures", deal_id, order="created_at")
    audit_rows = _query_audit_log_for_deal(client, deal_id)
    analyses = _query_analyses_for_deal(client, deal_id)

    return DealAuditView(
        deal_id=deal_id,
        decisions=[DecisionRow.model_validate(r) for r in decisions],
        disclosures=[DisclosureRow.model_validate(r) for r in disclosures],
        audit_log=[AuditEvent.model_validate(r) for r in audit_rows],
        analyses=[AnalysisStub.model_validate(r) for r in analyses],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _query_table_for_deal(
    client: Any,  # noqa: ANN401 — supabase client is untyped
    table: str,
    deal_id: UUID,
    *,
    order: str,
) -> list[dict[str, Any]]:
    """Pull every row from ``table`` where deal_id matches, oldest first.

    Returns an empty list on query failure — the audit view degrades
    gracefully rather than 500-ing on a transient sub-query.
    """
    try:
        result = (
            client.table(table)
            .select("*")
            .eq("deal_id", str(deal_id))
            .order(order, desc=False)
            .execute()
        )
    except Exception:
        _log.warning("audit.%s_query_failed deal_id=%s", table, deal_id)
        return []
    return list(result.data or [])


def _query_audit_log_for_deal(
    client: Any,  # noqa: ANN401
    deal_id: UUID,
) -> list[dict[str, Any]]:
    """audit_log has subject_type/subject_id, not deal_id directly.

    We pull rows where subject_type='deal' OR subject_type='document' OR
    subject_type='merchant' for the deal's document_id and the linked
    merchant. For now, only the document_id pull (deal_id == document_id)
    — the merchant-scoped audit story is broader and can land in a
    follow-up commit.
    """
    try:
        result = (
            client.table("audit_log")
            .select("*")
            .eq("subject_id", str(deal_id))
            .order("created_at", desc=False)
            .execute()
        )
    except Exception:
        _log.warning("audit.audit_log_query_failed deal_id=%s", deal_id)
        return []
    return list(result.data or [])


def _query_analyses_for_deal(
    client: Any,  # noqa: ANN401
    deal_id: UUID,
) -> list[dict[str, Any]]:
    """analyses.document_id == deal_id."""
    try:
        result = (
            client.table("analyses")
            .select("id, document_id, statement_period_start, statement_period_end, created_at")
            .eq("document_id", str(deal_id))
            .execute()
        )
    except Exception:
        _log.warning("audit.analyses_query_failed deal_id=%s", deal_id)
        return []
    return list(result.data or [])
