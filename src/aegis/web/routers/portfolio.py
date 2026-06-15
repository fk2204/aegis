"""Portfolio sub-router — operator analytics over the deal pipeline (M11 / U11).

Read-only aggregations across merchants, documents, audit_log, and
funder_replies. The math lives in
``aegis.deals.portfolio_analytics.compute_portfolio_metrics``; this
handler is the I/O layer that pulls the rows in the requested date
window and hands them to the pure aggregator.

Auth: same Cloudflare-Access posture as every other ``/ui/...`` route —
no bearer required; the SSO gate sits in front of the app in prod and
is bypassed on localhost dev.

PII: business_name renders in the per-deal recent-activity table per
the existing dashboard pattern (merchants table shows business names
today). It is NEVER written to a query-string or audit row from this
route — the route reads only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_decision_snapshot,
    get_disclosure_render_event_repository,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    get_submission_repository,
)
from aegis.audit import AuditLog
from aegis.compliance.render_events import DisclosureRenderEventRepository
from aegis.compliance.snapshot import (
    DecisionSnapshot,
    InMemoryDecisionSnapshot,
)
from aegis.deals.portfolio_analytics import (
    DateRange,
    compute_portfolio_metrics,
    resolve_date_range,
)
from aegis.deals.sprint3_portfolio import (
    APPROVAL_LOOKBACK_DAYS,
    compute_sprint3_metrics,
)
from aegis.funder_note_submissions.repository import (
    FunderNoteSubmissionRepository,
)
from aegis.funders.repository import FunderRepository
from aegis.merchants.repository import MerchantRepository
from aegis.storage import DocumentRepository
from aegis.submissions import SubmissionRepository
from aegis.web._templates import templates

router = APIRouter()


def _fetch_portfolio_data(
    audit: AuditLog,
    snapshot: DecisionSnapshot,
    date_range: DateRange,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(audit_rows, funder_reply_rows, decision_rows)`` for the window.

    Two paths:

      * Supabase-backed audit log → issue a ranged ``audit_log`` query,
        a ``funder_replies`` query, and a ``decisions`` query, all
        bounded to the window.
      * In-memory audit log (tests / dev) → read ``audit.entries``
        directly and ``InMemoryDecisionSnapshot.rows()`` for the
        snapshot. The in-memory FunderReplyRepository tracks rows on
        the same object, so the caller passes them via dependency
        override when the test needs replies. Fall back to ``[]``
        when not available.

    The split keeps the analytics layer pure (it takes rows, not repos)
    and gives tests one focused point to inject fixture data.
    """
    audit_rows: list[dict[str, Any]] = []
    reply_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []

    if hasattr(audit, "entries"):
        # In-memory branch — duck-typed at ``InMemoryAuditLog.entries``.
        entries: list[dict[str, Any]] = getattr(audit, "entries", [])
        # InMemoryAuditLog doesn't stamp created_at, so the window
        # filter is a no-op there. Tests supplying explicit timestamps
        # are honored via the filter below.
        for r in entries:
            ts = r.get("created_at")
            if ts is None:
                audit_rows.append(r)
                continue
            row_date = _coerce_audit_date(ts)
            if row_date is None:
                audit_rows.append(r)
            elif date_range.from_date <= row_date <= date_range.to_date:
                audit_rows.append(r)
        if isinstance(snapshot, InMemoryDecisionSnapshot):
            for r in snapshot.rows():
                ts = r.get("decided_at")
                if ts is None:
                    decision_rows.append(r)
                    continue
                row_date = _coerce_audit_date(ts)
                if row_date is None:
                    decision_rows.append(r)
                elif date_range.from_date <= row_date <= date_range.to_date:
                    decision_rows.append(r)
    else:
        # Supabase branch — issue the ranged query directly. The route
        # already imports get_supabase via aegis.audit; importing here
        # would create a circular path. Use a late local import.
        try:
            from aegis.db import get_supabase

            from_iso = date_range.from_date.isoformat()
            to_iso = date_range.to_date.isoformat()
            audit_result = (
                get_supabase()
                .table("audit_log")
                .select("*")
                .gte("created_at", from_iso)
                .lte("created_at", to_iso + "T23:59:59Z")
                .order("created_at", desc=True)
                .limit(5000)
                .execute()
            )
            audit_rows = cast(list[dict[str, Any]], audit_result.data or [])
            reply_result = (
                get_supabase()
                .table("funder_replies")
                .select("funder_id,deal_id,status,received_at")
                .gte("received_at", from_iso)
                .lte("received_at", to_iso + "T23:59:59Z")
                .limit(5000)
                .execute()
            )
            reply_rows = cast(list[dict[str, Any]], reply_result.data or [])
            decisions_result = (
                get_supabase()
                .table("decisions")
                .select("id,deal_id,decided_at,decision,state_code,score,score_factors")
                .gte("decided_at", from_iso)
                .lte("decided_at", to_iso + "T23:59:59Z")
                .order("decided_at", desc=True)
                .limit(5000)
                .execute()
            )
            decision_rows = cast(list[dict[str, Any]], decisions_result.data or [])
        except Exception:
            # Treat data fetch failure as an empty window rather than
            # 500-ing the page. The operator sees the empty state and
            # the structured log captures the failure for ops.
            from aegis.logger import get_logger

            get_logger(__name__).warning(
                "portfolio.fetch_failed window=%s..%s", date_range.from_date, date_range.to_date
            )
            audit_rows = []
            reply_rows = []
            decision_rows = []

    return audit_rows, reply_rows, decision_rows


def _coerce_audit_date(value: object) -> date | None:
    """Pull a ``date`` out of an audit row's ``created_at``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_view(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funders_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    docs_repo: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    render_events_repo: Annotated[
        DisclosureRenderEventRepository,
        Depends(get_disclosure_render_event_repository),
    ],
    submissions_repo: Annotated[SubmissionRepository, Depends(get_submission_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    from_: Annotated[
        str | None,
        Query(
            alias="from",
            description=("Window start (YYYY-MM-DD). Defaults to today minus 30 days."),
        ),
    ] = None,
    to: Annotated[
        str | None,
        Query(
            description=("Window end (YYYY-MM-DD). Defaults to today."),
        ),
    ] = None,
) -> HTMLResponse:
    """Portfolio analytics — pipeline funnel + funder approval rates +
    decisions by tier / state + recent activity + fraud catch rate.

    Date range:

      * ``?from=YYYY-MM-DD&to=YYYY-MM-DD`` narrows the window.
      * Default: last 30 days.
      * Window > 365 days is silently clamped to 365 (the operator's
        actual rendered window is shown in the page header so the cap
        is visible, never silent).

    Malformed dates → 400 with the parse error in the response body.
    """
    try:
        date_range = resolve_date_range(from_, to)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid date range: {exc}",
        ) from exc

    merchants_list = merchants_repo.list_all()
    funders_list = funders_repo.list_active()
    # Use a generous document limit so a busy month doesn't truncate
    # the fraud-catch denominator. The in-memory backend caps at the
    # explicit limit; the supabase backend honors the same.
    documents_list = docs_repo.list_documents(limit=5000)

    audit_rows, reply_rows, decision_rows = _fetch_portfolio_data(audit, snapshot, date_range)

    # Render-event records for the disclosure_render_queue tile (U21).
    # Defensive try/except — a repo whose backend is unreachable should
    # render the tile as a zero state rather than 500 the whole page.
    try:
        render_event_records = render_events_repo.list_in_window(
            from_date=date_range.from_date,
            to_date=date_range.to_date,
        )
    except Exception:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "portfolio.render_events_fetch_failed window=%s..%s",
            date_range.from_date,
            date_range.to_date,
        )
        render_event_records = []

    # U20: durable submissions table replaces the audit_log fallback for
    # per-funder submission counts. Same defensive try/except — table
    # outage should render the funder approval panel empty rather than
    # 500 the whole page.
    try:
        submissions_records = submissions_repo.list_in_window(
            from_date=date_range.from_date,
            to_date=date_range.to_date,
        )
    except Exception:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "portfolio.submissions_fetch_failed window=%s..%s",
            date_range.from_date,
            date_range.to_date,
        )
        submissions_records = []

    metrics = compute_portfolio_metrics(
        merchants=merchants_list,
        funders=funders_list,
        documents=documents_list,
        funder_reply_rows=reply_rows,
        audit_rows=audit_rows,
        date_range=date_range,
        decision_rows=decision_rows,
        submissions=submissions_records,
        render_events=render_event_records,
    )

    # ── Sprint 3 — funder-note-submissions analytics ───────────────
    # Pull a 90-day window of funder_note_submissions and derive the
    # six aggregate metrics for the Sprint 3 portfolio block. The
    # window matches APPROVAL_LOOKBACK_DAYS; the calendar-month
    # comparison inside compute_sprint3_metrics is naturally bounded
    # by the same window (this/last month fall well inside 90 days).
    # Defensive try/except mirrors the other portfolio fetches — a
    # repo outage renders the block empty, not a 500.
    now_dt = datetime.now(UTC)
    funder_note_submissions: list[Any] = []
    try:
        funder_note_submissions = funder_note_subs.list_in_window(
            from_dt=now_dt - timedelta(days=APPROVAL_LOOKBACK_DAYS),
            to_dt=now_dt,
        )
    except Exception:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "portfolio.funder_note_submissions_fetch_failed",
            exc_info=True,
        )

    # Build latest-decision-tier-per-merchant from already-loaded
    # decision_rows. The decisions schema keys tier at
    # ``score_factors.tier`` (see
    # ``aegis.deals.portfolio_analytics._decision_tier``); rows whose
    # deal_id maps to a document with no merchant_id (orphan upload)
    # are dropped so the dict is keyed solely by real merchants.
    doc_merchant_by_deal_id: dict[str, UUID] = {}
    for d in documents_list:
        if d.merchant_id is not None:
            doc_merchant_by_deal_id[str(d.id)] = d.merchant_id
    latest_decision_at: dict[UUID, str] = {}
    latest_decision_tier: dict[UUID, str] = {}
    for row in decision_rows:
        deal_id = str(row.get("deal_id", ""))
        merchant_id = doc_merchant_by_deal_id.get(deal_id)
        if merchant_id is None:
            continue
        factors = row.get("score_factors")
        tier = (
            factors.get("tier")
            if isinstance(factors, dict) and isinstance(factors.get("tier"), str)
            else None
        )
        if tier is None:
            continue
        decided_at = str(row.get("decided_at") or "")
        prev = latest_decision_at.get(merchant_id, "")
        if decided_at >= prev:
            latest_decision_at[merchant_id] = decided_at
            latest_decision_tier[merchant_id] = tier

    sprint3_metrics = compute_sprint3_metrics(
        submissions=list(funder_note_submissions),
        merchants=merchants_list,
        documents=documents_list,
        latest_decision_tier_by_merchant=latest_decision_tier,
        now=now_dt,
    )

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "portfolio.html.j2",
            {
                "active": "Portfolio",
                "metrics": metrics,
                "sprint3_metrics": sprint3_metrics,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )
