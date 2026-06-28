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
from aegis.web.routers.dashboard import _score_to_tier_letter

router = APIRouter()

# Recent-outcome window for column 4 (2026-06-28). 90 days gives the
# operator the broader read on the funded/outcome bucket — matches the
# deals-list 90-day window so the two surfaces agree on "recent
# response activity". The narrow 30-day window the kanban shipped with
# was hiding ~60d of outcomes the operator still cared about for
# trend / re-engagement.
_OUTCOME_RECENT_DAYS: Final[int] = 90

# Window over which a merchant is considered "already submitted" and
# therefore excluded from the Ready column. Matches the dashboard
# key-numbers ``_KEY_NUMBERS_READY_WINDOW_DAYS`` and the deal-flow
# convention (a merchant submitted >30 days ago is fair game for
# re-submission with a fresh statement).
_READY_SUBMISSION_EXCLUSION_DAYS: Final[int] = 30

# Window for column 3 "Submitted" — pending submissions in the last
# 60 days. Older pending submissions are stale enough to belong on
# the deal-detail page, not the at-a-glance kanban.
_SUBMITTED_PENDING_WINDOW_DAYS: Final[int] = 60

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

# Submission-window scan: covers the recent-outcome window
# (_OUTCOME_RECENT_DAYS, 90 days as of 2026-06-28) plus a safety margin
# so a submission whose ``submitted_at`` is older than 90 days but whose
# ``responded_at`` is within 90 still surfaces. 270 = 90 outcome window
# + 180 margin (worst-case turnaround time on a stalled funder response).
_SUBMISSION_SCAN_DAYS: Final[int] = 270


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
    # Cheap estimated advance derived from
    # ``aegis.scoring.score._suggested_max_advance(monthly_revenue,
    # paper_grade)``. ``None`` when paper_grade or monthly_revenue
    # is missing — the kanban can't fabricate a sizing in that case.
    estimated_advance: Decimal | None
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
    """Column 2 — merchants with a ``proceed`` doc, no submission in the
    last ``_READY_SUBMISSION_EXCLUSION_DAYS``, and ``ofac_is_clear !=
    False`` (i.e. clear OR not-yet-checked).

    Window logic update (2026-06-28): the prior 365-day exclusion
    permanently parked any merchant who had been submitted once; a
    re-uploaded fresh statement could not surface in the kanban for a
    full year. Operators wanted the column to refresh once enough time
    had passed for a re-submission to be reasonable — 30 days is the
    accepted convention (matches the Today key-numbers banner and the
    deal-flow assumption that funder responses land inside 30 days).
    """
    # Lazy import — keeps the pipeline column rebuild light and avoids
    # pulling the scoring module's transitive imports into every
    # request that touches the kanban polling endpoint.
    from aegis.scoring.score import _suggested_max_advance

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

    # Recent-submission exclusion: merchants submitted in the last
    # ``_READY_SUBMISSION_EXCLUSION_DAYS`` days drop out of this column
    # (they belong in Submitted or Outcome).
    window_start = now - timedelta(days=_READY_SUBMISSION_EXCLUSION_DAYS)
    submissions = funder_note_subs.list_in_window(from_dt=window_start, to_dt=now)
    recently_submitted_merchant_ids = {s.merchant_id for s in submissions}

    rows: list[_ReadyRow] = []
    for mid, d in latest_by_merchant.items():
        if mid in recently_submitted_merchant_ids:
            continue
        analysis: AnalysisRow | None = analyses_by_doc.get(d.id)

        # OFAC gate: skip merchants whose OFAC check has explicitly
        # returned ``False`` (sanctioned). ``None`` (not yet checked)
        # is allowed — the dossier surfaces the chip and the operator
        # decides; the column shouldn't disappear just because the
        # background-check cron hasn't visited the merchant yet.
        try:
            merchant = merchants_repo.get(mid)
        except MerchantNotFoundError:
            continue
        if merchant.ofac_is_clear is False:
            continue
        # 2026-06-28 perf — was ``_compute_merchant_tier(merchant, docs,
        # ofac)`` which ran score_deal() per merchant: profiling showed
        # _build_ready = 11,609ms for 8 rows (~1.5s/row) because every
        # call collected the merchant's analyzed docs + ran multi-month
        # scoring + OFAC + Track A/B compute for a single letter chip.
        # Derive from the already-on-row ``fraud_score`` via the
        # cheap-band helper instead. Accuracy trade: the band now
        # reflects the legacy single-axis fraud_score rather than the
        # multi-month track-aware score; operators drill into the
        # dossier for the authoritative tier. Same trade-off the
        # today_pipeline tier-breakdown made (also uses
        # _score_to_tier_letter against the decisions table).
        paper_grade: str | None = (
            _score_to_tier_letter(d.fraud_score) if d.fraud_score is not None else None
        )

        true_revenue: Decimal | None = analysis.true_revenue if analysis else None
        action_chip = _action_chip_from_narrator(analysis.narrator_summary if analysis else None)
        # Cheap estimated-advance proxy. ``monthly_revenue`` lives on
        # the analysis (parser aggregate); the formula matches the
        # legacy ``_suggested_max_advance`` used everywhere else in
        # the scorer so the kanban + dossier agree. ``None`` when
        # either input is missing — render as em-dash.
        monthly_revenue = analysis.monthly_revenue if analysis else None
        if paper_grade is not None and monthly_revenue is not None:
            try:
                estimated_advance: Decimal | None = _suggested_max_advance(
                    monthly_revenue, paper_grade
                )
            except (KeyError, ValueError):
                estimated_advance = None
        else:
            estimated_advance = None

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
                estimated_advance=estimated_advance,
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
    """Column 3 — pending submissions grouped by merchant, within the
    ``_SUBMITTED_PENDING_WINDOW_DAYS`` window (60 days as of 2026-06-28).

    A merchant with multiple pending submissions to different funders
    collapses to one row whose ``funder_names`` lists each. Oldest first
    so the most-overdue submission tops the column. Pending submissions
    OLDER than the window age out — they belong on the per-merchant
    dossier history, not in the at-a-glance kanban.
    """
    window_start = now - timedelta(days=_SUBMITTED_PENDING_WINDOW_DAYS)
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
    """Column 4 — terminal-status submissions whose response landed in the
    trailing ``_OUTCOME_RECENT_DAYS`` (90 days as of 2026-06-28)."""
    # Scan window is the outcome window + the safety margin (the
    # submission could have been submitted before the response window).
    # Keep _SUBMISSION_SCAN_DAYS for the scan, then filter by the
    # _OUTCOME_RECENT_DAYS response cutoff.
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
