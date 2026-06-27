"""Pipeline sub-router — ``GET /ui/pipeline`` kanban view (4 stages).

Surfaces the operator's deal flow at a glance, kanban-style. Four
columns:

  1. **Docs In / Parsing** — merchants with at least one ``pending``
     document. Oldest upload first so the most-overdue parse lands at
     the top.
  2. **Ready to Review** — merchants with at least one ``proceed`` doc
     AND zero funder_note_submissions. Sorted by paper-grade ASC then
     true_revenue DESC so the cleanest big deals top the list.
  3. **Submitted** — funder_note_submissions in ``pending`` status,
     grouped by merchant, oldest first. Days-since-submission is the
     urgency cue.
  4. **Outcome Recorded** — submissions with terminal status
     (``approved`` / ``declined`` / ``countered``) whose ``responded_at``
     falls in the last 30 days.

Auto-refresh: the page polls ``GET /ui/pipeline/refresh`` every 60s and
swaps the kanban grid in-place via HTMX. Same partial powers the
initial render and the refresh, so the two surfaces can't drift.

Nav link: appended to ``_topstrip.html.j2`` under the Today area as a
top-level entry, not nested in a dropdown. Available to all roles.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.funder_note_submissions import (
    FunderNoteSubmissionRepository,
    FunderNoteSubmissionRow,
)
from aegis.funders.repository import FunderNotFoundError, FunderRepository
from aegis.merchants.repository import (
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.scoring.ofac import OFACClient
from aegis.storage import AnalysisRow, DocumentRepository, DocumentRow
from aegis.web._templates import templates
from aegis.web.routers.dashboard import _compute_merchant_tier

router = APIRouter()

# Recent-outcome window for column 4. 30 days matches the operator's
# "did we hear back recently?" mental model without making the column
# unbounded (a year-old outcome shouldn't appear next to a fresh one).
_OUTCOME_RECENT_DAYS: Final[int] = 30

# Soft cap on per-column row count so a backlog spike doesn't render
# 1000 cards into the DOM. Cards beyond the cap surface a "+N more"
# tail row so the operator knows the column is deeper than what they
# see.
_COLUMN_DISPLAY_CAP: Final[int] = 50

# Document scan limit — covers ~5 months of upload backlog at
# ~100 deals/month. Matches the convention used by the Today dashboard
# (``_compute_stale_deals``) so the two surfaces agree on "what
# documents do we consider live".
_DOCUMENT_SCAN_LIMIT: Final[int] = 500

# Submission-window scan: covers the recent-outcome window plus a
# safety margin so a submission whose ``submitted_at`` is older than 30
# days but whose ``responded_at`` is within 30 still surfaces.
_SUBMISSION_SCAN_DAYS: Final[int] = 180


_ActionChip = Literal["submit_now", "call_first", "request_documents", "decline"]


@dataclass(frozen=True)
class _DocsInRow:
    """One row in column 1 — a merchant with at least one pending doc."""

    merchant_id: str
    merchant_label: str
    pending_count: int
    oldest_uploaded_at: datetime


@dataclass(frozen=True)
class _ReadyRow:
    """One row in column 2 — proceed-status deal awaiting first submission."""

    merchant_id: str
    merchant_label: str
    paper_grade: str | None  # "A".."F" or None when scorer can't run
    true_revenue: Decimal | None
    top_funder_match: str | None
    action_chip: _ActionChip | None


@dataclass(frozen=True)
class _SubmittedRow:
    """One row in column 3 — open submissions to funder(s), no response yet."""

    merchant_id: str
    merchant_label: str
    funder_names: list[str]
    oldest_submitted_at: datetime


@dataclass(frozen=True)
class _OutcomeRow:
    """One row in column 4 — recently-responded submission."""

    merchant_id: str
    merchant_label: str
    funder_name: str
    outcome: str
    responded_at: datetime


@dataclass(frozen=True)
class _PipelineColumns:
    """Bundle of all 4 column lists + their cap-truncation tail counts.

    ``tail_*`` is the count of rows beyond ``_COLUMN_DISPLAY_CAP`` that
    were dropped from the visible list; the template renders a "+N more"
    footer when non-zero.
    """

    docs_in: list[_DocsInRow]
    ready: list[_ReadyRow]
    submitted: list[_SubmittedRow]
    outcomes: list[_OutcomeRow]
    docs_in_tail: int
    ready_tail: int
    submitted_tail: int
    outcomes_tail: int


def _merchant_label(
    merchants_repo: MerchantRepository,
    merchant_id: UUID,
) -> str:
    """Resolve merchant_id to its display name, with a short-hash fallback."""
    try:
        return merchants_repo.get(merchant_id).business_name
    except MerchantNotFoundError:
        return f"merchant {str(merchant_id)[:8]}"


def _build_docs_in(
    docs: DocumentRepository,
    merchants_repo: MerchantRepository,
    *,
    now: datetime,
) -> list[_DocsInRow]:
    """Column 1 — merchants with at least one ``pending`` document.

    Grouped by merchant. Each row carries the count of pending docs and
    the OLDEST pending upload time (so sorting oldest-first surfaces the
    most-overdue parse — same urgency convention as the Today dashboard's
    stale-deals card).
    """
    pending_docs = docs.list_documents(
        parse_status="pending",
        limit=_DOCUMENT_SCAN_LIMIT,
    )
    by_merchant: dict[UUID, list[DocumentRow]] = defaultdict(list)
    for d in pending_docs:
        if d.merchant_id is None:
            continue
        by_merchant[d.merchant_id].append(d)

    rows: list[_DocsInRow] = []
    for mid, group in by_merchant.items():
        oldest = min(d.uploaded_at for d in group)
        rows.append(
            _DocsInRow(
                merchant_id=str(mid),
                merchant_label=_merchant_label(merchants_repo, mid),
                pending_count=len(group),
                oldest_uploaded_at=oldest,
            )
        )
    # Oldest first — same urgency convention as Today / stale-deals.
    rows.sort(key=lambda r: r.oldest_uploaded_at)
    # ``now`` is consumed by the template's "time since" filter; binding
    # it here keeps the query layer independent of clock-time formatting.
    _ = now
    return rows


def _action_chip_from_narrator(
    narrator_summary: dict[str, object] | None,
) -> _ActionChip | None:
    """Translate the cached narrator summary's ``action`` key into the chip
    enum the template colours. Missing / unknown values collapse to None
    (the chip is omitted)."""
    if not narrator_summary:
        return None
    action_raw = narrator_summary.get("action")
    if not isinstance(action_raw, str):
        return None
    action_norm = action_raw.strip().lower().replace(" ", "_")
    if action_norm in (
        "submit_now",
        "call_first",
        "request_documents",
        "decline",
    ):
        # mypy can't infer Literal narrowing from a runtime tuple check
        # without a cast, but the membership above guarantees the value.
        return action_norm  # type: ignore[return-value]
    return None


def _build_ready(
    docs: DocumentRepository,
    merchants_repo: MerchantRepository,
    funder_note_subs: FunderNoteSubmissionRepository,
    ofac: OFACClient | None,
    *,
    now: datetime,
) -> list[_ReadyRow]:
    """Column 2 — merchants with a ``proceed`` doc AND no submissions yet.

    "No submissions yet" is read from ``funder_note_submissions`` —
    a merchant with at least one row in that table is in column 3 or 4,
    not here.
    """
    proceed_docs = docs.list_documents(
        parse_status="proceed",
        limit=_DOCUMENT_SCAN_LIMIT,
    )
    # Keep the LATEST proceed doc per merchant — its analysis carries the
    # canonical true_revenue + narrator_summary.
    latest_by_merchant: dict[UUID, DocumentRow] = {}
    for d in proceed_docs:
        if d.merchant_id is None:
            continue
        if d.merchant_id not in latest_by_merchant:
            latest_by_merchant[d.merchant_id] = d

    if not latest_by_merchant:
        return []

    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in latest_by_merchant.values()])

    # Submitted-merchant exclusion: pull a wide submission window and
    # build a set of merchant_ids that already have at least one row.
    # 365-day window because the contract says "no submission yet" —
    # a merchant who was submitted long ago and re-uploaded a fresh
    # statement should NOT appear here; their old submission row keeps
    # them out of column 2 forever (or until the operator records an
    # outcome AND starts a fresh deal cycle, which is a future surface).
    window_start = now - timedelta(days=365)
    submissions = funder_note_subs.list_in_window(from_dt=window_start, to_dt=now)
    submitted_merchant_ids = {s.merchant_id for s in submissions}

    rows: list[_ReadyRow] = []
    for mid, d in latest_by_merchant.items():
        if mid in submitted_merchant_ids:
            continue
        analysis: AnalysisRow | None = analyses_by_doc.get(d.id)

        # Paper grade — reuses the dashboard's tier computation so the
        # two surfaces can't disagree.
        try:
            merchant = merchants_repo.get(mid)
        except MerchantNotFoundError:
            continue
        paper_grade = _compute_merchant_tier(merchant, docs, ofac)

        true_revenue: Decimal | None = analysis.true_revenue if analysis else None
        action_chip = _action_chip_from_narrator(analysis.narrator_summary if analysis else None)
        # ``top_funder_match`` is intentionally None at the column level —
        # running the full funder-match pass per merchant is too expensive
        # for a 60s-polling kanban. The dossier surfaces the same data
        # on click-through; the column row carries an em-dash placeholder
        # so the layout stays stable.
        top_funder_match: str | None = None

        rows.append(
            _ReadyRow(
                merchant_id=str(mid),
                merchant_label=merchant.business_name,
                paper_grade=paper_grade,
                true_revenue=true_revenue,
                top_funder_match=top_funder_match,
                action_chip=action_chip,
            )
        )

    # Sort: paper_grade ASC (A < B < C < D < F < None), then
    # true_revenue DESC (bigger deals first within a grade band).
    _grade_order = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}

    def _sort_key(r: _ReadyRow) -> tuple[int, Decimal]:
        grade = _grade_order.get(r.paper_grade or "", 99)
        rev_neg = -(r.true_revenue or Decimal("0"))
        return (grade, rev_neg)

    rows.sort(key=_sort_key)
    return rows


def _build_submitted(
    funder_note_subs: FunderNoteSubmissionRepository,
    merchants_repo: MerchantRepository,
    funders_repo: FunderRepository,
    *,
    now: datetime,
) -> list[_SubmittedRow]:
    """Column 3 — pending submissions grouped by merchant.

    A merchant with multiple pending submissions to different funders
    collapses to one row whose ``funder_names`` lists each. Oldest first
    so the most-overdue submission tops the column.
    """
    window_start = now - timedelta(days=_SUBMISSION_SCAN_DAYS)
    submissions = funder_note_subs.list_in_window(from_dt=window_start, to_dt=now)

    by_merchant: dict[UUID, list[FunderNoteSubmissionRow]] = defaultdict(list)
    for s in submissions:
        if s.status != "pending":
            continue
        by_merchant[s.merchant_id].append(s)

    # One pass to collect every funder_id we need to label.
    funder_ids = {s.funder_id for group in by_merchant.values() for s in group}
    funder_name_by_id: dict[UUID, str] = {}
    for fid in funder_ids:
        try:
            funder_name_by_id[fid] = funders_repo.get(fid).name
        except FunderNotFoundError:
            funder_name_by_id[fid] = f"funder {str(fid)[:8]}"

    rows: list[_SubmittedRow] = []
    for mid, group in by_merchant.items():
        names = sorted({funder_name_by_id.get(s.funder_id, "—") for s in group})
        oldest = min(s.submitted_at for s in group)
        rows.append(
            _SubmittedRow(
                merchant_id=str(mid),
                merchant_label=_merchant_label(merchants_repo, mid),
                funder_names=names,
                oldest_submitted_at=oldest,
            )
        )
    rows.sort(key=lambda r: r.oldest_submitted_at)
    return rows


def _build_outcomes(
    funder_note_subs: FunderNoteSubmissionRepository,
    merchants_repo: MerchantRepository,
    funders_repo: FunderRepository,
    *,
    now: datetime,
) -> list[_OutcomeRow]:
    """Column 4 — terminal-status submissions whose response landed in 30d."""
    window_start = now - timedelta(days=_SUBMISSION_SCAN_DAYS)
    cutoff = now - timedelta(days=_OUTCOME_RECENT_DAYS)
    submissions = funder_note_subs.list_in_window(from_dt=window_start, to_dt=now)

    eligible = [
        s
        for s in submissions
        if s.status in ("approved", "declined", "countered")
        and s.responded_at is not None
        and s.responded_at >= cutoff
    ]

    funder_ids = {s.funder_id for s in eligible}
    funder_name_by_id: dict[UUID, str] = {}
    for fid in funder_ids:
        try:
            funder_name_by_id[fid] = funders_repo.get(fid).name
        except FunderNotFoundError:
            funder_name_by_id[fid] = f"funder {str(fid)[:8]}"

    rows: list[_OutcomeRow] = []
    for s in eligible:
        # mypy: ``responded_at`` is filtered non-None in the comprehension
        # above; fall back to ``s.submitted_at`` for the type-narrowing edge
        # case where mypy can't see through the filter (the fallback is
        # never taken at runtime).
        responded = s.responded_at if s.responded_at is not None else s.submitted_at
        rows.append(
            _OutcomeRow(
                merchant_id=str(s.merchant_id),
                merchant_label=_merchant_label(merchants_repo, s.merchant_id),
                funder_name=funder_name_by_id.get(s.funder_id, "—"),
                outcome=s.status,
                responded_at=responded,
            )
        )
    rows.sort(key=lambda r: r.responded_at, reverse=True)
    return rows


def _truncate(rows: list[object], cap: int) -> tuple[list[object], int]:
    """Cap a row list, returning ``(visible, tail_count)``."""
    if len(rows) <= cap:
        return rows, 0
    return rows[:cap], len(rows) - cap


def _build_pipeline(
    *,
    docs: DocumentRepository,
    merchants_repo: MerchantRepository,
    funder_note_subs: FunderNoteSubmissionRepository,
    funders_repo: FunderRepository,
    ofac: OFACClient | None,
    now: datetime,
) -> _PipelineColumns:
    """Assemble all 4 columns. Single call per render."""
    docs_in_all = _build_docs_in(docs, merchants_repo, now=now)
    ready_all = _build_ready(docs, merchants_repo, funder_note_subs, ofac, now=now)
    submitted_all = _build_submitted(funder_note_subs, merchants_repo, funders_repo, now=now)
    outcomes_all = _build_outcomes(funder_note_subs, merchants_repo, funders_repo, now=now)

    docs_in_visible, docs_in_tail = _truncate(list(docs_in_all), _COLUMN_DISPLAY_CAP)
    ready_visible, ready_tail = _truncate(list(ready_all), _COLUMN_DISPLAY_CAP)
    submitted_visible, submitted_tail = _truncate(list(submitted_all), _COLUMN_DISPLAY_CAP)
    outcomes_visible, outcomes_tail = _truncate(list(outcomes_all), _COLUMN_DISPLAY_CAP)

    return _PipelineColumns(
        # mypy: the _truncate helper takes a list[object] for variance; the
        # narrowed lists below are still the concrete row dataclasses.
        docs_in=[r for r in docs_in_visible if isinstance(r, _DocsInRow)],
        ready=[r for r in ready_visible if isinstance(r, _ReadyRow)],
        submitted=[r for r in submitted_visible if isinstance(r, _SubmittedRow)],
        outcomes=[r for r in outcomes_visible if isinstance(r, _OutcomeRow)],
        docs_in_tail=docs_in_tail,
        ready_tail=ready_tail,
        submitted_tail=submitted_tail,
        outcomes_tail=outcomes_tail,
    )


def _render_columns(
    request: Request,
    *,
    columns: _PipelineColumns,
    now: datetime,
) -> HTMLResponse:
    """Render the 4-column partial (used by both initial + refresh)."""
    return templates.TemplateResponse(
        request,
        "_pipeline_column.html.j2",
        {
            "columns": columns,
            "now": now,
        },
    )


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline_index(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    funders_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> HTMLResponse:
    """Full kanban page render.

    Wraps the 4-column partial in the base chrome (topstrip, statusbar).
    The grid auto-polls ``/ui/pipeline/refresh`` every 60s so a long-open
    tab stays current without an operator-side reload.
    """
    now = datetime.now(UTC)
    columns = _build_pipeline(
        docs=docs,
        merchants_repo=merchants_repo,
        funder_note_subs=funder_note_subs,
        funders_repo=funders_repo,
        ofac=ofac,
        now=now,
    )
    return templates.TemplateResponse(
        request,
        "pipeline.html.j2",
        {
            "columns": columns,
            "now": now,
            "active": "Pipeline",
        },
    )


@router.get("/pipeline/refresh", response_class=HTMLResponse)
async def pipeline_refresh(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    funders_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> HTMLResponse:
    """HTMX swap target: re-render only the 4-column grid.

    The page-level ``hx-get`` swaps the grid in place every 60s. Returns
    the same partial the initial render uses so the surfaces can't drift.
    """
    now = datetime.now(UTC)
    columns = _build_pipeline(
        docs=docs,
        merchants_repo=merchants_repo,
        funder_note_subs=funder_note_subs,
        funders_repo=funders_repo,
        ofac=ofac,
        now=now,
    )
    return _render_columns(request, columns=columns, now=now)


__all__ = ["router"]
