"""GET /audit/deal/{deal_id} — full evidence trail per deal (mp Phase 2 + 7).

Master plan §12 (Phase 2): "returns all decisions chronologically, all
disclosures with delivery proof, all audit_log events, all linked
statements/analyses."

Master plan §17 (Phase 7) extensions:
* Filter audit_log results by date range, event type, actor.
* CSV + JSON export endpoints with the same filters (parity-tested).

Authn: bearer token (consistent with the rest of the API). RLS on the
underlying tables denies anon role; service_role bypasses, which is what
the supabase-py client carries.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
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
    actor_email: str | None = None
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
# Filter param shape
# ---------------------------------------------------------------------------


# CSV columns are the canonical export shape. JSON export uses the same
# field set so parity tests can iterate one source of truth.
_AUDIT_EXPORT_COLUMNS: tuple[str, ...] = (
    "created_at",
    "actor",
    "actor_email",
    "action",
    "subject_type",
    "subject_id",
    "details",
)


def _audit_filter_params(
    date_from: Annotated[
        datetime | None,
        Query(
            alias="from",
            description="ISO 8601 lower bound on created_at (inclusive).",
        ),
    ] = None,
    date_to: Annotated[
        datetime | None,
        Query(
            alias="to",
            description="ISO 8601 upper bound on created_at (inclusive).",
        ),
    ] = None,
    event_type: Annotated[
        list[str] | None,
        Query(
            description="Audit `action` to include; repeat for multiple values.",
        ),
    ] = None,
    actor: Annotated[
        list[str] | None,
        Query(
            description=(
                "Operator email, system actor ('worker', 'api', "
                "'audit_archiver', etc.); repeat for multiple values."
            ),
        ),
    ] = None,
) -> "AuditFilter":
    """Materialize the filter query-string into a typed object.

    FastAPI dependency: the audit-list and export routes inject this so the
    filter shape is enforced in one place.
    """
    return AuditFilter(
        date_from=date_from,
        date_to=date_to,
        event_types=tuple(event_type or ()),
        actors=tuple(actor or ()),
    )


class AuditFilter(BaseModel):
    """Filters applied to the audit_log subset of the deal audit view.

    Immutable. The set semantics:
      * date_from/date_to are inclusive bounds on created_at.
      * event_types: empty -> no filter; non-empty -> action IN (...).
      * actors: empty -> no filter; non-empty -> actor IN (...) OR
        actor_email IN (...). Matching against actor_email lets the
        operator filter by their own email regardless of which system
        actor wrote the row.

    Unknown actor / event_type values return an empty result set, never
    a 400 — the regulator-facing export must not error on "no matching
    audit rows for this filter."
    """

    model_config = ConfigDict(frozen=True)

    date_from: datetime | None = None
    date_to: datetime | None = None
    event_types: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()

    def matches(self, row: dict[str, Any]) -> bool:
        """Apply the filter to one audit row dict.

        Used by the in-Python filter pass after the Supabase query has
        narrowed the date range server-side. Belt + suspenders: even if
        the server-side narrowing is off (e.g. a fake client in tests),
        the in-Python pass still produces the right answer.
        """
        if self.date_from is not None or self.date_to is not None:
            created_raw = row.get("created_at")
            if not created_raw:
                # Rows without a created_at can never satisfy a date
                # filter. Exclude rather than fall through.
                return False
            created = _parse_dt(created_raw)
            if created is None:
                return False
            if self.date_from is not None and created < self.date_from:
                return False
            if self.date_to is not None and created > self.date_to:
                return False
        if self.event_types:
            if row.get("action") not in self.event_types:
                return False
        if self.actors:
            row_actor = row.get("actor")
            row_email = row.get("actor_email")
            if row_actor not in self.actors and row_email not in self.actors:
                return False
        return True


def _parse_dt(value: str) -> datetime | None:
    """ISO 8601 -> datetime, tolerant of trailing 'Z' and missing tz."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/deal/{deal_id}",
    response_model=DealAuditView,
    summary=(
        "Full audit trail for a deal — decisions + disclosures + audit_log + "
        "analyses. Audit_log subset accepts date / event_type / actor filters."
    ),
)
def get_deal_audit(
    deal_id: UUID,
    filt: Annotated[AuditFilter, Depends(_audit_filter_params)],
) -> DealAuditView:
    client = get_supabase()
    _assert_deal_exists(client, deal_id)

    decisions = _query_table_for_deal(client, "decisions", deal_id, order="decided_at")
    disclosures = _query_table_for_deal(client, "disclosures", deal_id, order="created_at")
    audit_rows = _query_audit_log_for_deal(client, deal_id, filt=filt)
    analyses = _query_analyses_for_deal(client, deal_id)

    return DealAuditView(
        deal_id=deal_id,
        decisions=[DecisionRow.model_validate(r) for r in decisions],
        disclosures=[DisclosureRow.model_validate(r) for r in disclosures],
        audit_log=[AuditEvent.model_validate(r) for r in audit_rows],
        analyses=[AnalysisStub.model_validate(r) for r in analyses],
    )


@router.get(
    "/deal/{deal_id}/export.json",
    summary="Audit_log rows for a deal as JSON. Same filters as the main route.",
)
def export_deal_audit_json(
    deal_id: UUID,
    filt: Annotated[AuditFilter, Depends(_audit_filter_params)],
) -> Response:
    client = get_supabase()
    _assert_deal_exists(client, deal_id)
    rows = _query_audit_log_for_deal(client, deal_id, filt=filt)
    body = json.dumps(
        {
            "deal_id": str(deal_id),
            "row_count": len(rows),
            "columns": list(_AUDIT_EXPORT_COLUMNS),
            "rows": [_normalize_export_row(r) for r in rows],
        },
        default=str,
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="audit-{deal_id}.json"'
            ),
        },
    )


@router.get(
    "/deal/{deal_id}/export.csv",
    summary="Audit_log rows for a deal as CSV. Same filters as the main route.",
)
def export_deal_audit_csv(
    deal_id: UUID,
    filt: Annotated[AuditFilter, Depends(_audit_filter_params)],
) -> Response:
    client = get_supabase()
    _assert_deal_exists(client, deal_id)
    rows = _query_audit_log_for_deal(client, deal_id, filt=filt)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_AUDIT_EXPORT_COLUMNS)
    for r in rows:
        norm = _normalize_export_row(r)
        writer.writerow(_csv_cell(norm[c]) for c in _AUDIT_EXPORT_COLUMNS)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="audit-{deal_id}.csv"'
            ),
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_deal_exists(
    client: Any,  # noqa: ANN401 — supabase client is untyped
    deal_id: UUID,
) -> None:
    """404 if the document (deal) doesn't exist; 503 on DB outage."""
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="deal_not_found"
        )


def _normalize_export_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project an audit_log row down to the canonical export columns.

    Missing columns become None so CSV + JSON shapes are identical. The
    ``details`` JSON column is preserved as-is in JSON; the CSV path
    serializes it via ``_csv_cell``.
    """
    return {col: row.get(col) for col in _AUDIT_EXPORT_COLUMNS}


def _csv_cell(value: Any) -> str:  # noqa: ANN401 — heterogeneous cell types
    """Render one audit-row cell into a CSV-safe string.

    None -> empty string; dict / list -> compact JSON. UUIDs / datetimes
    flow through str() naturally.
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


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
    *,
    filt: AuditFilter | None = None,
) -> list[dict[str, Any]]:
    """audit_log has subject_type/subject_id, not deal_id directly.

    Filters (date range, event_type, actor) apply in the in-Python pass
    after the Supabase query. The Supabase narrowing is best-effort —
    only the subject_id filter goes server-side, mirroring the original
    behavior; the rest run in-process so tests' fake clients (which
    swallow chained .eq() calls) still produce the right result set.
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

    rows: list[dict[str, Any]] = list(result.data or [])
    if filt is None:
        return rows
    return [r for r in rows if filt.matches(r)]


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
