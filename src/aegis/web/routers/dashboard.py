"""Dashboard sub-router — Today, manual-review queue, deals lifecycle.

Routes:
  * ``GET /ui/``        — Today dashboard (funnel, attention queue, recent activity)
  * ``GET /ui/review``  — Manual-review document queue (one card per doc)
  * ``GET /ui/deals``   — Deal lifecycle table (one row per merchant, latest doc)

Extracted from ``router.py`` during R4.1. Several private helpers in
this module are re-imported by ``aegis.web.router`` and re-exported so
existing test imports (``from aegis.web.router import
_build_attention_groups`` / ``_build_review_queue_cards`` /
``_compute_merchant_tier``) keep working.

The tier-lookup chain (``_compute_merchant_tier`` → multi-month scoring
→ ``_collect_analyzed_for_merchant``) lives here too. The collect
helper + bundling helpers moved to ``aegis.web._router_helpers`` during
R4.1 finish-part-4 (the merchants split), so the lazy back-import the
cycle previously required is no longer needed.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_funder_note_submission_repository,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.compliance.obligations import (
    build_compliance_attention_section,
    get_compliance_obligation_repository,
)
from aegis.funder_note_submissions import FunderNoteSubmissionRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.ops.shadow_review import build_shadow_review_attention_section
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring.multi_month import (
    score_input_multi_month as _score_input_multi_month,
)
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
from aegis.storage import AnalysisRow, DocumentRepository, DocumentRow
from aegis.web._attention_card import (
    CATEGORY_LABELS,
    AttentionCard,
    DocumentPatternContext,
    PatternIndex,
    ReviewQueueCard,
    categorize_flags,
    derive_fraud_band,
)
from aegis.web._flag_labels import humanize_audit_action
from aegis.web._templates import templates

router = APIRouter()


_REVIEW_QUEUE_DISPLAY_CAP: Final[int] = 200

# Submission-count window for the deals-table "Submitted to N funders"
# column + the "Pending submission" filter chip. 90 days matches the
# funder-list approval-rate window (funders.py) so the two surfaces
# agree on "active pipeline".
_DEALS_LIST_SUBMISSION_WINDOW_DAYS: Final[int] = 90


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
) -> HTMLResponse:
    """Today dashboard — live KPIs sourced from Supabase / in-memory repos.

    Funnel: parse-status histogram with proportional bar widths so the
    operator sees how many docs are at each stage. "Sent to funders"
    and "Funded" are sourced from the audit_log action histogram (no
    table for these yet — Phase 7C work).

    Attention queue: most-recent ``manual_review`` documents joined to
    their merchant for context.

    Recent activity: last 10 ``audit_log`` rows. The audit table is
    masked at write time so PII never lands here either.
    """
    parse_counts = docs.count_by_parse_status()
    merchant_total = merchants_repo.count_total()

    proceed = parse_counts.get("proceed", 0)
    review = parse_counts.get("review", 0)
    manual_review = parse_counts.get("manual_review", 0)
    pending = parse_counts.get("pending", 0)
    error = parse_counts.get("error", 0)
    parsed_total = proceed + review + manual_review + error
    in_pipeline = parsed_total + pending

    recent_activity_rows = audit.list_recent(limit=10)
    recent_activity = [
        {
            "actor": r.get("actor") or "—",
            "action": humanize_audit_action(
                r.get("action") or "—",
                r.get("details") if isinstance(r.get("details"), dict) else None,
            ),
            "subject_type": r.get("subject_type") or "",
            "subject_id": r.get("subject_id") or "",
            "time_short": _format_activity_time(r.get("created_at")),
        }
        for r in recent_activity_rows
    ]

    attention_docs = docs.list_documents(parse_status="manual_review", limit=40)
    attention = _build_attention_groups(attention_docs, merchants_repo, docs, max_groups=8)
    # 2026-06-28 perf — tier enrichment dropped from the dashboard hot
    # path. Per-card profiling showed
    # ``_enrich_attention_card_with_tier x 8 = 11,967ms`` -- 60%+ of the
    # 16-second dashboard render. score_deal() per merchant ran a
    # multi-month scoring pass + OFAC check + Track A/B compute for a
    # decorative letter chip the template gates on truthy ``g.tier``.
    # Cards still render fine without it (the {% if g.tier %} branch
    # just doesn't fire). Revisit when the score_deal path is async /
    # cached so 8 enrichments fan out concurrently instead of serially.
    # Refs: ``_enrich_attention_card_with_tier`` in this module (kept
    # for the Review Queue card builder which can afford the cost).
    _ = ofac  # held in signature for the dependency wiring + future use

    submitted_count = sum(
        1 for r in recent_activity_rows if r.get("action") == "deal.submit_to_funders"
    )
    funded_count = sum(1 for r in recent_activity_rows if r.get("action") == "deal.funded")

    funnel_rows = _build_funnel_rows(
        intake_count=merchant_total,
        docs_uploaded=in_pipeline,
        parsed=parsed_total,
        underwritten=proceed + review,
        submitted=submitted_count,
        funded=funded_count,
        declined=manual_review + error,
    )

    # Three-column Today dashboard (2026-06-16) — additional aggregates.
    # Per CLAUDE.md "every aggregate exposes its source IDs": each new
    # count below is paired with a *_source_ids list so the dossier
    # drill-down contract holds.
    now_utc = datetime.now(UTC)

    # 2026-06-28 perf — fetch the documents window ONCE up front and
    # pass to EVERY helper that needs it. The prior implementation hit
    # ``docs.list_documents`` 5+ times across this route (stale_deals,
    # today_pipeline, key_numbers x2, monthly_comparison). Each call
    # was a 740ms+ SELECT * against the wide documents table — they
    # were stacking to a 23-second dashboard load (operator report
    # 2026-06-28 15:13). One cached fetch at the widest limit (500)
    # covers every helper that filters in Python downstream.
    all_recent_docs = docs.list_documents(limit=500)

    (
        stale_deals_count,
        stale_deals_source_merchant_ids,
        stale_attention_cards,
    ) = _compute_stale_deals(docs, merchants_repo, now=now_utc, all_recent_docs=all_recent_docs)
    (
        pending_funder_count,
        pending_funder_source_submission_ids,
        pending_funder_cards,
    ) = _compute_pending_funder_responses(funder_note_subs, merchants_repo, now=now_utc)
    # Compliance deadlines attention card — obligations with
    # next_due_date within the 90-day horizon. Color buckets: red <=14,
    # amber <=30, yellow <=60. Sourced from the dedicated obligation
    # tracker repository (memory/Supabase toggle from settings).
    (
        compliance_count,
        compliance_source_obligation_ids,
        compliance_cards,
    ) = build_compliance_attention_section(
        get_compliance_obligation_repository(),
    )
    # Shadow signals attention card — documents parsed in the last 7
    # days with at least one ``[SHADOW] *`` flag on ``all_flags``.
    # Surfaces the weekly cron's intake (`run_shadow_review_cron`) on
    # the dashboard so the corpus-validation review for promoting a
    # shadow detector to live is one click away from Today. `count` is
    # the DISTINCT document count, paired with `source_document_ids`
    # per the CLAUDE.md aggregate-with-source-ids rule.
    (
        shadow_review_count,
        shadow_review_source_document_ids,
        shadow_review_cards,
    ) = build_shadow_review_attention_section(docs=docs, merchants=merchants_repo)
    today_pipeline = _compute_today_pipeline(
        docs, funder_note_subs, now=now_utc, all_recent_docs=all_recent_docs
    )
    today_recent_activity = _build_today_recent_activity(recent_activity_rows)

    key_numbers = _compute_key_numbers(
        merchant_total=merchant_total,
        pending_count=pending,
        proceed_count=proceed,
        funder_note_subs=funder_note_subs,
        docs=docs,
        merchants_repo=merchants_repo,
        now=now_utc,
        all_recent_docs=all_recent_docs,
    )
    monthly_comparison = _compute_monthly_comparison(
        docs=docs,
        now=now_utc,
        all_recent_docs=all_recent_docs,
    )

    # Quick-action buttons are static — declared here so the template
    # iterates a list rather than hardcoding three blocks. Labels are
    # operator-facing; URLs go to existing routes (no new routes added).
    quick_actions = [
        {
            "label": "Upload statement",
            "href": "/ui/upload",
            "test_id": "today-action-upload",
        },
        {
            "label": "Add funder",
            "href": "/ui/funders/import",
            "test_id": "today-action-add-funder",
        },
        {
            "label": "New intake",
            "href": "/ui/intake",
            "test_id": "today-action-new-intake",
        },
    ]

    return templates.TemplateResponse(
        request,
        "index.html.j2",
        {
            "now": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "merchant_total": merchant_total,
            "in_pipeline": in_pipeline,
            "manual_review_count": manual_review,
            "proceed_count": proceed,
            "review_count": review,
            "pending_count": pending,
            "error_count": error,
            "funnel_rows": funnel_rows,
            "attention": attention,
            "category_labels": CATEGORY_LABELS,
            "recent_activity": recent_activity,
            # NEW — Today three-column dashboard context.
            "stale_deals_count": stale_deals_count,
            "stale_deals_source_merchant_ids": stale_deals_source_merchant_ids,
            "stale_deals_cards": stale_attention_cards,
            "pending_funder_count": pending_funder_count,
            "pending_funder_source_submission_ids": (pending_funder_source_submission_ids),
            "pending_funder_cards": pending_funder_cards,
            "compliance_count": compliance_count,
            "compliance_source_obligation_ids": compliance_source_obligation_ids,
            "compliance_cards": compliance_cards,
            "shadow_review_count": shadow_review_count,
            "shadow_review_source_document_ids": (shadow_review_source_document_ids),
            "shadow_review_cards": shadow_review_cards,
            "today_pipeline": today_pipeline,
            "today_recent_activity": today_recent_activity,
            "quick_actions": quick_actions,
            # 2026-06-28 refresh — key numbers banner + 3-month strip.
            "key_numbers": key_numbers,
            "monthly_comparison": monthly_comparison,
        },
    )


def _build_funnel_rows(
    *,
    intake_count: int,
    docs_uploaded: int,
    parsed: int,
    underwritten: int,
    submitted: int,
    funded: int,
    declined: int,
) -> list[dict[str, Any]]:
    """Compute proportional bar widths for the pipeline funnel.

    Width is relative to ``intake_count`` (the widest bar). All other
    stages are smaller-or-equal. Empty pipeline collapses to zero-width
    bars rather than rendering garbage.
    """
    base = max(intake_count, 1)

    def _w(n: int) -> int:
        return min(100, int((n / base) * 100)) if base > 0 else 0

    return [
        {"label": "Intake", "count": intake_count, "width": _w(intake_count), "cls": ""},
        {"label": "Docs uploaded", "count": docs_uploaded, "width": _w(docs_uploaded), "cls": ""},
        {"label": "Parsed", "count": parsed, "width": _w(parsed), "cls": "accent"},
        {"label": "Underwritten", "count": underwritten, "width": _w(underwritten), "cls": ""},
        {"label": "Sent to funders", "count": submitted, "width": _w(submitted), "cls": "pos"},
        {"label": "Funded", "count": funded, "width": _w(funded), "cls": "pos"},
        {"label": "Declined", "count": declined, "width": _w(declined), "cls": "neg"},
    ]


_STALE_DOC_THRESHOLD: Final[timedelta] = timedelta(days=7)
_PENDING_RESPONSE_THRESHOLD: Final[timedelta] = timedelta(days=5)
# Soft cap on the attention-queue secondary lists — keeps the card
# render bounded when an operator has a long-tail backlog.
_TODAY_LIST_CAP: Final[int] = 8


def _start_of_today_utc(now: datetime) -> datetime:
    """Truncate ``now`` to midnight UTC. Pure helper for the today windows."""
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def _compute_stale_deals(
    docs: DocumentRepository,
    merchants_repo: MerchantRepository,
    *,
    now: datetime,
    all_recent_docs: list[DocumentRow] | None = None,
) -> tuple[int, list[str], list[dict[str, Any]]]:
    """Surface merchants whose most-recent document is 7+ days stale.

    Returns ``(count, source_merchant_ids, cards)``. ``cards`` are
    template-ready dicts the right-rail attention queue iterates; the
    ``source_merchant_ids`` list is the audit trail that pairs with the
    aggregate count per CLAUDE.md auditability rule.

    Repository returns documents most-recent-first; we walk the list and
    keep the first occurrence per merchant — that's the latest upload.
    Merchants with no documents at all are NOT surfaced here (no upload
    activity to be stale against; they belong on the intake queue).
    """
    cutoff = now - _STALE_DOC_THRESHOLD
    # Re-uses the shared route-level fetch when provided (saves a
    # SELECT * FROM documents LIMIT 500 round-trip). 500 covers ~5
    # months of upload backlog at ~100 deals/month — well above the
    # realistic working window for "is this deal stalled?".
    all_docs = all_recent_docs if all_recent_docs is not None else docs.list_documents(limit=500)
    latest_by_merchant: dict[UUID, DocumentRow] = {}
    for d in all_docs:
        if d.merchant_id is None or d.merchant_id in latest_by_merchant:
            continue
        latest_by_merchant[d.merchant_id] = d

    stale_pairs: list[tuple[UUID, DocumentRow]] = [
        (mid, d) for mid, d in latest_by_merchant.items() if d.uploaded_at < cutoff
    ]
    # Oldest-stalest first so the worker sees the most urgent items at the top.
    stale_pairs.sort(key=lambda pair: pair[1].uploaded_at)

    source_ids: list[str] = [str(mid) for mid, _ in stale_pairs]
    cards: list[dict[str, Any]] = []
    for mid, d in stale_pairs[:_TODAY_LIST_CAP]:
        label = f"merchant {str(mid)[:8]}"
        try:
            merchant = merchants_repo.get(mid)
            label = merchant.business_name
        except MerchantNotFoundError:
            pass
        days_stale = max(0, (now - d.uploaded_at).days)
        cards.append(
            {
                "merchant_id": str(mid),
                "merchant_label": label,
                "needs": (
                    f"No new document in {days_stale} days — chase the merchant"
                    if days_stale > 0
                    else "Latest document is stale — review or re-request"
                ),
                "href": f"/ui/merchants/{mid}",
                "test_id": "today-attn-stale",
            }
        )
    return len(stale_pairs), source_ids, cards


def _compute_pending_funder_responses(
    funder_note_subs: FunderNoteSubmissionRepository,
    merchants_repo: MerchantRepository,
    *,
    now: datetime,
) -> tuple[int, list[str], list[dict[str, Any]]]:
    """Surface ``pending`` funder-note submissions older than 5 days.

    ``list_in_window`` is the cheapest read on the repository — pull a
    wide window (90 days back), then filter to ``status == "pending"``
    AND ``submitted_at < now - 5d``. Returns ``(count, source_ids, cards)``
    paired per CLAUDE.md auditability.
    """
    cutoff = now - _PENDING_RESPONSE_THRESHOLD
    window_start = now - timedelta(days=90)
    submissions = funder_note_subs.list_in_window(from_dt=window_start, to_dt=now)
    stale_pending = [s for s in submissions if s.status == "pending" and s.submitted_at < cutoff]
    # Oldest first — same urgency convention as stale deals.
    stale_pending.sort(key=lambda s: s.submitted_at)

    source_ids: list[str] = [str(s.id) for s in stale_pending]
    cards: list[dict[str, Any]] = []
    for s in stale_pending[:_TODAY_LIST_CAP]:
        label = f"merchant {str(s.merchant_id)[:8]}"
        try:
            merchant = merchants_repo.get(s.merchant_id)
            label = merchant.business_name
        except MerchantNotFoundError:
            pass
        days_pending = max(0, (now - s.submitted_at).days)
        cards.append(
            {
                "merchant_id": str(s.merchant_id),
                "submission_id": str(s.id),
                "merchant_label": label,
                "needs": (f"Funder silent {days_pending} days — follow up or reroute"),
                "href": f"/ui/merchants/{s.merchant_id}",
                "test_id": "today-attn-pending-funder",
            }
        )
    return len(stale_pending), source_ids, cards


def _score_to_tier_letter(score: float | int | None) -> str:
    """Five-band tier derivation: A>=90, B>=75, C>=60, D>=40, F<40.

    Fallback when the row doesn't carry an explicit tier letter — the
    ``decisions`` table only persists the integer score, so the Today
    tier-breakdown pills derive the letter here. Matches the convention
    documented in the task brief; the per-deal scorer (`score_deal`)
    remains the authoritative source for individual cards.
    """
    if score is None:
        return "F"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "F"
    if s >= 90:
        return "A"
    if s >= 75:
        return "B"
    if s >= 60:
        return "C"
    if s >= 40:
        return "D"
    return "F"


def _compute_today_pipeline(
    docs: DocumentRepository,
    funder_note_subs: FunderNoteSubmissionRepository,
    *,
    now: datetime,
    all_recent_docs: list[DocumentRow] | None = None,
) -> dict[str, Any]:
    """Pipeline KPIs scoped to today (UTC).

    Returns ``{deals_scored, tiers: {letter -> count}, funnel_stages,
    deals_scored_source_decision_ids}``. The five funnel stages map to
    Uploaded / Parsed / Scored / Submitted / Decision.

    Decisions + funnel counts read from Supabase directly on the
    Supabase backend; the in-memory backend falls back to zeros for
    decisions (no in-memory snapshot is wired into this surface and a
    fresh-zero is safer than a stale value).
    """
    start = _start_of_today_utc(now)

    # --- documents-today: uploaded + parsed --------------------------
    # Re-uses shared route-level fetch when provided; saves another
    # SELECT * FROM documents LIMIT 500 round-trip per dashboard render.
    docs_pool = all_recent_docs if all_recent_docs is not None else docs.list_documents(limit=500)
    todays_docs = [d for d in docs_pool if d.uploaded_at >= start]
    uploaded_today = len(todays_docs)
    parsed_today = sum(1 for d in todays_docs if d.parsed_at is not None)

    # --- decisions-today: count + tier breakdown ----------------------
    deals_scored = 0
    decision_ids: list[str] = []
    tiers: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    try:
        from aegis.db import get_supabase

        result = (
            get_supabase()
            .table("decisions")
            .select("id,decided_at,score")
            .gte("decided_at", start.isoformat())
            .order("decided_at", desc=True)
            .limit(1000)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        deals_scored = len(rows)
        for r in rows:
            rid = r.get("id")
            if rid is not None:
                decision_ids.append(str(rid))
            letter = _score_to_tier_letter(r.get("score"))
            tiers[letter] = tiers.get(letter, 0) + 1
    except Exception:
        # In-memory backend / supabase outage — surface zero, never 500
        # the Today page. The structured log captures the failure.
        from aegis.logger import get_logger

        get_logger(__name__).warning("today.decisions_fetch_failed")

    # --- submissions-today / responses-today --------------------------
    todays_submissions = funder_note_subs.list_in_window(from_dt=start, to_dt=now)
    submitted_today = len(todays_submissions)
    decision_today = sum(
        1
        for s in todays_submissions
        if s.status in ("approved", "declined")
        and s.responded_at is not None
        and s.responded_at >= start
    )

    funnel_stages = [
        {"label": "Uploaded", "count": uploaded_today},
        {"label": "Parsed", "count": parsed_today},
        {"label": "Scored", "count": deals_scored},
        {"label": "Submitted", "count": submitted_today},
        {"label": "Decision", "count": decision_today},
    ]

    return {
        "deals_scored": deals_scored,
        "deals_scored_source_decision_ids": decision_ids,
        "tiers": tiers,
        "funnel_stages": funnel_stages,
    }


_KEY_NUMBERS_REVENUE_WINDOW_DAYS: Final[int] = 7
_MONTHLY_COMPARISON_LOOKBACK_DOCS: Final[int] = 200
# Trailing 6 calendar months — wider than the legacy 3 so the operator
# sees a meaningful trend window. Two quarters of revenue cadence is the
# minimum needed to read seasonality vs. drift.
_MONTHLY_COMPARISON_BUCKETS: Final[int] = 6
# Submission-window for "Ready to submit" in the key-numbers banner;
# matches the Pipeline column-2 / deals-list 90-day convention so the
# three surfaces (Today banner, deals list, kanban) agree on "active".
_KEY_NUMBERS_READY_WINDOW_DAYS: Final[int] = 30


def _compute_key_numbers(
    *,
    merchant_total: int,
    pending_count: int,
    proceed_count: int,
    funder_note_subs: FunderNoteSubmissionRepository,
    docs: DocumentRepository,
    merchants_repo: MerchantRepository,
    now: datetime,
    all_recent_docs: list[DocumentRow] | None = None,
) -> dict[str, Any]:
    """Top-of-dashboard key-numbers banner.

    Cheap reads only — three counts come in already-computed from the
    route; ``submitted_this_week`` is a single ``list_in_window`` call
    on the submissions repo; ``avg_revenue_this_week`` averages
    ``analyses.true_revenue`` across documents parsed in the last 7
    days (bounded by ``_KEY_NUMBERS_REVENUE_WINDOW_DAYS``).

    "Active deals" = ``merchants_repo.list_all()`` length. The repo's
    ``list_all`` already excludes soft-deleted rows (migration 065).
    AEGIS does not carry a ``status='disqualified'`` enum today; the
    closest equivalent — soft-delete — is already filtered. Future
    "disqualified" status (when wired) lands here.

    "Ready to submit" = merchants with a ``proceed`` document AND no
    funder_note_submission in the last ``_KEY_NUMBERS_READY_WINDOW_DAYS``
    days. Matches the Pipeline column-2 / kanban "Ready to Review"
    definition so the two surfaces agree (a merchant that's already
    been submitted to a funder is NOT "ready to submit again").
    OFAC clearance is NOT gated here — ``ofac_is_clear=False`` already
    suppresses the funder grid at the dossier, so the merchant can't be
    submitted from there; counting them in this banner inflates the
    headline number without adding actionable work.
    """
    from decimal import Decimal

    window_start = now - timedelta(days=_KEY_NUMBERS_REVENUE_WINDOW_DAYS)
    submissions = funder_note_subs.list_in_window(from_dt=window_start, to_dt=now)
    submitted_this_week = len(submissions)

    # --- Ready-to-submit refinement ----------------------------------
    # proceed-status docs joined to "no funder submission in the
    # trailing 30 days" — the dossier-honest definition. Filters the
    # shared ``all_recent_docs`` window in Python rather than running a
    # second ``list_documents(parse_status="proceed")`` round-trip.
    ready_window_start = now - timedelta(days=_KEY_NUMBERS_READY_WINDOW_DAYS)
    recent_submissions = funder_note_subs.list_in_window(from_dt=ready_window_start, to_dt=now)
    recently_submitted_merchant_ids = {s.merchant_id for s in recent_submissions}
    if all_recent_docs is None:
        all_recent_docs = docs.list_documents(limit=500)
    proceed_docs = [d for d in all_recent_docs if d.parse_status == "proceed"]
    proceed_merchant_ids = {d.merchant_id for d in proceed_docs if d.merchant_id is not None}
    ready_to_submit_count = len(proceed_merchant_ids - recently_submitted_merchant_ids)

    # --- Active-deals ------------------------------------------------
    # ``merchant_total`` comes in from ``count_total`` which already
    # excludes soft-deleted rows. Keep the same value here so the
    # legacy stats strip and the key-numbers banner agree.
    active_deals = merchant_total
    _ = merchants_repo  # held in signature for future status filtering

    # --- Avg revenue (7d) — re-uses shared docs window ---------------
    week_docs = [
        d for d in all_recent_docs if d.parsed_at is not None and d.parsed_at >= window_start
    ]
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in week_docs])
    revenues = [a.true_revenue for a in analyses_by_doc.values() if a.true_revenue is not None]
    if revenues:
        avg_revenue = sum(revenues, Decimal("0")) / Decimal(len(revenues))
    else:
        avg_revenue = None

    # Source IDs for the auditability rule: every aggregate ships with
    # the list of contributing IDs so the dossier drill-down contract
    # holds. ``avg_revenue`` is keyed by document, the rest by merchant.
    return {
        "active_deals": active_deals,
        "pending_parse": pending_count,
        # Kept for callers that still read the unfiltered field (no
        # current callers, but a templates-side regression should not
        # rename to a brittle key).
        "ready_to_submit_proceed_count": proceed_count,
        "ready_to_submit": ready_to_submit_count,
        "ready_to_submit_source_merchant_ids": sorted(
            str(mid) for mid in (proceed_merchant_ids - recently_submitted_merchant_ids)
        ),
        "submitted_this_week": submitted_this_week,
        "submitted_this_week_source_submission_ids": [str(s.id) for s in submissions],
        "avg_revenue_this_week": avg_revenue,
        "avg_revenue_this_week_source_document_ids": [
            str(doc_id) for doc_id, a in analyses_by_doc.items() if a.true_revenue is not None
        ],
    }


def _compute_monthly_comparison(
    *,
    docs: DocumentRepository,
    now: datetime,
    all_recent_docs: list[DocumentRow] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate the trailing-6-months portfolio view from
    ``analyses.monthly_breakdown`` across the last ``_MONTHLY_COMPARISON_LOOKBACK_DOCS``
    proceed-eligible documents. Newest-first; each row carries a trend
    arrow vs. the prior month.

    ``monthly_breakdown`` is a per-document list of dicts shaped
    ``{month: "YYYY-MM", deposits, withdrawals, avg_balance,
    nsf_count}`` (Decimals as strings — see parser/aggregate.py). Here
    we union those per-month rows across every recent doc, sum
    deposits / nsf_count, average avg_balance, and return the most
    recent ``_MONTHLY_COMPARISON_BUCKETS`` months newest-first.

    Each row also carries ``source_document_ids: list[str]`` — the
    documents whose ``monthly_breakdown`` contributed to that bucket —
    so the dossier drill-down contract holds per the CLAUDE.md
    "every aggregate ships with source IDs" rule.

    Per-month "true revenue" (deposits net of transfers + MCA paybacks)
    is NOT available from the parser today. ``analyses.true_revenue``
    is a single portfolio-level Decimal computed across the full
    statement period; the per-month rows carry only gross deposits +
    nsf_count + avg_balance + withdrawals (gross, not category-split).
    Splitting per-month true_revenue requires the classifier to bucket
    transfers / mca_paybacks per month and the aggregator to project
    those into ``monthly_breakdown`` — a parser-side change not in this
    sprint's scope.

    TODO(parser): emit per-month ``transfers`` + ``mca_paybacks`` on
    ``monthly_breakdown`` rows so the dashboard can compute per-month
    ``true_revenue = deposits - transfers - mca_paybacks``. Until then
    this strip surfaces "Gross deposits" (the operator-honest label) so
    the operator knows they're looking at the pre-net figure.

    Returns an empty list when no documents carry a populated
    ``monthly_breakdown`` — the template hides the strip in that case.
    """
    from collections import defaultdict
    from decimal import Decimal, InvalidOperation

    # ``now`` reserved for future "trailing-N-months from today" windowing;
    # current impl returns the most-recent N populated months across all
    # documents regardless of calendar offset.
    _ = now

    # Re-uses the shared route-level fetch when provided (saves a 740ms
    # SELECT * round-trip). Falls back to its own query for callers that
    # invoke this helper standalone (tests / future surfaces).
    recent_docs = all_recent_docs or docs.list_documents(limit=_MONTHLY_COMPARISON_LOOKBACK_DOCS)
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in recent_docs])

    deposits_by_month: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    nsf_by_month: defaultdict[str, int] = defaultdict(int)
    balance_samples: defaultdict[str, list[Decimal]] = defaultdict(list)
    sources_by_month: defaultdict[str, list[str]] = defaultdict(list)

    for doc_id, analysis in analyses_by_doc.items():
        for row in analysis.monthly_breakdown or []:
            month = row.get("month")
            if not month:
                continue
            try:
                deposits_by_month[month] += Decimal(row.get("deposits") or "0")
                nsf_by_month[month] += int(row.get("nsf_count") or "0")
                balance_samples[month].append(Decimal(row.get("avg_balance") or "0"))
            except (InvalidOperation, ValueError):
                continue
            sources_by_month[month].append(str(doc_id))

    if not deposits_by_month:
        return []

    sorted_months = sorted(deposits_by_month.keys(), reverse=True)[:_MONTHLY_COMPARISON_BUCKETS]
    # Build oldest-first so each entry can compare against its predecessor;
    # we reverse for the newest-first render contract right before return.
    in_chrono = list(reversed(sorted_months))
    out: list[dict[str, Any]] = []
    prev_deposits: Decimal | None = None
    # Operator-visible threshold below which a delta reads as "flat" not
    # "up/down" — 2% of the prior month. Cheap noise filter; keeps a
    # $50 wobble on a $50,000 month from rendering as a downward arrow.
    flat_threshold_pct = Decimal("0.02")

    chrono_rows: list[dict[str, Any]] = []
    for month in in_chrono:
        deposits = deposits_by_month[month]
        samples = balance_samples[month]
        avg_balance = (
            sum(samples, Decimal("0")) / Decimal(len(samples)) if samples else Decimal("0")
        )
        # Trend arrow vs. prior chronological month. First-month-of-window
        # carries no arrow (no comparator) — the template renders an empty
        # span so layout stays stable.
        if prev_deposits is None or prev_deposits == 0:
            trend: str = ""
        else:
            delta = deposits - prev_deposits
            threshold = prev_deposits.copy_abs() * flat_threshold_pct
            if delta.copy_abs() <= threshold:
                trend = "flat"
            elif delta > 0:
                trend = "up"
            else:
                trend = "down"
        # Human label: "Apr 2026". Falls back to the raw key on parse
        # failure so the strip still renders something.
        try:
            year, month_num = month.split("-")
            label = datetime(int(year), int(month_num), 1, tzinfo=UTC).strftime("%b %Y")
        except (ValueError, IndexError):
            label = month
        chrono_rows.append(
            {
                "label": label,
                "deposits": deposits,
                "deposits_source_ids": sources_by_month[month],
                "nsf_count": nsf_by_month[month],
                "adb": avg_balance,
                "trend": trend,
            }
        )
        prev_deposits = deposits

    # Newest-first for render (matches the prior contract).
    out = list(reversed(chrono_rows))
    return out


def _build_today_recent_activity(
    rows: list[dict[str, Any]], limit: int = 5
) -> list[dict[str, Any]]:
    """Last ``limit`` audit_log rows trimmed to single-line summaries.

    ``rows`` is the same ``audit.list_recent`` payload the legacy "Day's
    log" panel consumes; here we take the first ``limit`` (already
    newest-first), humanize the action, and drop the actor / subject
    columns so the right-rail row stays a single line of text.
    """
    out: list[dict[str, Any]] = []
    for r in rows[:limit]:
        out.append(
            {
                "action": humanize_audit_action(
                    r.get("action") or "—",
                    r.get("details") if isinstance(r.get("details"), dict) else None,
                ),
                "time_short": _format_activity_time(r.get("created_at")),
            }
        )
    return out


def _build_attention_groups(
    documents: list[DocumentRow],
    merchants_repo: MerchantRepository,
    docs: DocumentRepository,
    *,
    max_groups: int = 8,
) -> list[AttentionCard]:
    """Group manual_review documents by merchant for the Today attention queue.

    Input is assumed most-recent-first (the list_documents contract).
    Output preserves that ordering — the first card is the one whose
    most recent document was uploaded most recently.

    Each ``AttentionCard`` surfaces:
      * merchant_id / merchant_label
      * merchant_state / merchant_naics / requested_amount (chunk A new fields)
      * worst_fraud_score — max across the merchant's docs in the queue
      * fraud_band — clear/review/decline/unknown derived from worst score
      * tier — reserved for chunks B/C (None in chunk A)
      * doc_count
      * unique_flags — flat deduplicated list of raw flag strings
        (legacy field; the chunk-A index.html.j2 template still
        consumes this. Chunk B replaces the chip loop with category-
        grouped rendering off ``flags`` and drops this field.)
      * flags — ``CategorizedFlags`` with decline_class lifted to the
        top and the rest grouped by glossary category in display order
      * documents — per-doc dicts (document_id, fraud_score,
        uploaded_at, flags), most-recent first within the group

    Documents with merchant_id=None bucket under a single "—" card so
    they stay visible rather than being scattered as multiple unlabeled
    rows.

    Chunk 3: builds a per-merchant ``PatternIndex`` from the contributing
    docs' ``AnalysisRow.pattern_analysis`` caches (migration 032), with
    cross-doc filename tagging on each source row. The index decorates
    every ``HumanFlag`` whose code matches an emitted Pattern so the
    chip template renders an inline ``<details>`` drill-down. Docs whose
    cache is None (legacy pre-chunk-2 rows) contribute no entries and
    their flags degrade to plain spans — graceful by construction.
    """
    # Pre-fetch all docs' analyses in one batched query. Then batch the
    # transactions fetch for only the docs whose pattern_analysis is
    # populated (legacy rows have analysis.pattern_analysis is None and
    # contribute no PatternIndex entries — skip the round-trip).
    # 2026-06-28 perf: replaced per-doc list_transactions(d.id) loop in
    # _build_merchant_pattern_index (was 8-24 PostgREST round-trips per
    # dashboard render) with one in.(...) query across all eligible doc
    # IDs at the top of the helper.
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in documents])
    txn_doc_ids = [
        d.id
        for d in documents
        if (a := analyses_by_doc.get(d.id)) is not None and a.pattern_analysis is not None
    ]
    transactions_by_doc: dict[UUID, list[ClassifiedTransaction]] = (
        docs.list_transactions_for_documents(txn_doc_ids) if txn_doc_ids else {}
    )

    # 2026-06-28 perf — batch the merchant fetch. Was N x merchants_repo.get(key)
    # (one PostgREST round-trip per unique merchant in the queue, ~100ms each
    # x 8 cards = ~800ms). The new ``get_many_by_ids`` collapses that to one
    # ``in.(...)`` query. Missing merchants simply don't appear in the dict,
    # mirroring the absent-key semantics the per-card branch below already
    # handled via MerchantNotFoundError.
    candidate_merchant_ids: list[UUID] = []
    for d in documents:
        if d.merchant_id is not None and d.merchant_id not in candidate_merchant_ids:
            candidate_merchant_ids.append(d.merchant_id)
    merchants_by_id = merchants_repo.get_many_by_ids(candidate_merchant_ids)

    groups: dict[UUID | None, dict[str, Any]] = {}
    group_docs: dict[UUID | None, list[DocumentRow]] = {}

    for d in documents:
        key = d.merchant_id
        if key not in groups:
            if len(groups) >= max_groups:
                continue
            label = "—"
            merchant: MerchantRow | None = None
            if key is not None:
                merchant = merchants_by_id.get(key)
                if merchant is not None:
                    label = merchant.business_name
                else:
                    label = f"merchant {str(key)[:8]}"
            groups[key] = {
                "merchant_id": str(key) if key is not None else None,
                "merchant_label": label,
                "merchant_state": merchant.state if merchant else None,
                "merchant_naics": merchant.industry_naics if merchant else None,
                "requested_amount": merchant.requested_amount if merchant else None,
                # Migration 034 status — None for the unlinked "—"
                # bucket and for missing-merchant cards (deleted), so
                # the template can guard with a simple truthiness test.
                "merchant_status": merchant.status if merchant else None,
                "documents": [],
                "_seen_flags": [],
            }
            group_docs[key] = []

        group_docs[key].append(d)
        flags = list(d.all_flags) if d.all_flags else []
        groups[key]["documents"].append(
            {
                "document_id": str(d.id),
                "fraud_score": d.fraud_score,
                "uploaded_at": d.uploaded_at.strftime("%Y-%m-%d %H:%M"),
                "flags": flags,
            }
        )
        seen: list[str] = groups[key]["_seen_flags"]
        for f in flags:
            if f not in seen:
                seen.append(f)

    out: list[AttentionCard] = []
    for key, g in groups.items():
        scores = [doc["fraud_score"] for doc in g["documents"] if doc["fraud_score"] is not None]
        worst = max(scores) if scores else None
        pattern_index = _build_merchant_pattern_index(
            group_docs[key], analyses_by_doc, transactions_by_doc, docs
        )
        out.append(
            AttentionCard(
                merchant_id=g["merchant_id"],
                merchant_label=g["merchant_label"],
                merchant_state=g["merchant_state"],
                merchant_naics=g["merchant_naics"],
                requested_amount=g["requested_amount"],
                worst_fraud_score=worst,
                fraud_band=derive_fraud_band(worst),
                tier=None,
                doc_count=len(g["documents"]),
                documents=g["documents"],
                flags=categorize_flags(g["_seen_flags"], pattern_index=pattern_index),
                merchant_status=g["merchant_status"],
            )
        )
    return out


def _build_merchant_pattern_index(
    docs_in_group: list[DocumentRow],
    analyses_by_doc: dict[UUID, AnalysisRow],
    transactions_cache: dict[UUID, list[ClassifiedTransaction]],
    docs: DocumentRepository,
) -> PatternIndex:
    """Build a per-merchant PatternIndex across the group's docs.

    ``transactions_cache`` is shared across groups so a doc that
    appeared in an earlier group (shouldn't happen in practice since
    documents partition by merchant_id, but the cache is cheap) won't
    re-query. Transactions are only fetched for docs whose analysis
    cache is populated — legacy rows skip the round-trip.
    """
    contexts: list[DocumentPatternContext] = []
    for d in docs_in_group:
        analysis = analyses_by_doc.get(d.id)
        if analysis is None or analysis.pattern_analysis is None:
            contexts.append(
                DocumentPatternContext(
                    document_id=d.id,
                    filename=d.original_filename,
                    analysis=None,
                    transactions=[],
                )
            )
            continue
        if d.id not in transactions_cache:
            # Fallback for any caller that didn't pre-batch (or for a
            # doc that was somehow missing from the batch). Today's
            # caller pre-populates the cache via
            # list_transactions_for_documents, so this branch is dead
            # on the hot path.
            transactions_cache[d.id] = docs.list_transactions(d.id)
        contexts.append(
            DocumentPatternContext(
                document_id=d.id,
                filename=d.original_filename,
                analysis=analysis,
                transactions=transactions_cache[d.id],
            )
        )
    return PatternIndex.build_for_merchant(contexts)


def _compute_merchant_tier(
    merchant: MerchantRow,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> str | None:
    """Run ``score_deal`` on a merchant and return the deal tier letter.

    Shared by ``_enrich_attention_card_with_tier`` (Today, one call per
    merchant card) and ``_build_review_queue_cards`` (Review Queue, one
    call per merchant cached across the merchant's docs in the queue).

    Falls back to ``None`` on any failure — orphaned merchant, no
    analyzed docs yet, OFAC stale, score_deal raising on partial data,
    or any other exception. Tier is decorative on both card surfaces; a
    missing letter must never block the queue render. Unforeseen
    failures log a structured WARN so the silence isn't total.
    """
    # Migration 034 guard: non-finalized merchants (provisional /
    # needs_manual_naming) carry a placeholder business_name. Running
    # score_deal would invoke OFAC against the placeholder and write a
    # spurious compliance record. Skip — tier=None reads the same as
    # "not enough data yet" in the existing renderers.
    if not merchant.is_finalized:
        return None
    # Direct import — _collect_analyzed_for_merchant lifted to
    # _router_helpers during R4.1 finish-part-4 (the merchants split).
    # No cycle: _router_helpers does not import any sub-router.
    from aegis.web._router_helpers import _collect_analyzed_for_merchant

    try:
        items = _collect_analyzed_for_merchant(docs, merchant.id, bundle=None)
        if not items:
            return None
        score_input = _score_input_multi_month(merchant, items)
        # U33 — feed Track A/B verdicts so the tier letter reflects the
        # ``AEGIS_SCORING_ENGINE=track_abc`` decline path when active.
        # Failure to compute verdicts falls back to None (legacy engine).
        from aegis.scoring_v2.score_deal_inputs import (
            compute_score_deal_track_inputs,
        )

        _tier_documents = [d for d, _ in items]
        _tier_analyses_by_doc = {d.id: a for d, a in items}
        track_a_verdict, track_b_band = compute_score_deal_track_inputs(
            documents=_tier_documents,
            list_transactions=docs.list_transactions,
            analyses_by_doc=_tier_analyses_by_doc,
            merchant_id=merchant.id,
        )
        score_result = score_deal(
            score_input,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except (MerchantNotFoundError, OFACStaleError, ValueError):
        return None
    except Exception as exc:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "card.tier_lookup_failed merchant_id=%s err=%s",
            merchant.id,
            exc.__class__.__name__,
        )
        return None
    return score_result.tier


def _enrich_attention_card_with_tier(
    card: AttentionCard,
    merchants_repo: MerchantRepository,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> AttentionCard:
    """Decorate a Today attention card with its merchant's deal tier.

    Returns a new card via ``dataclasses.replace`` — ``AttentionCard``
    is frozen. Falls back to leaving ``tier=None`` on any failure (see
    ``_compute_merchant_tier``).
    """
    if card.merchant_id is None:
        return card
    try:
        merchant_uuid = UUID(card.merchant_id)
        merchant = merchants_repo.get(merchant_uuid)
    except (MerchantNotFoundError, ValueError):
        return card
    tier = _compute_merchant_tier(merchant, docs, ofac)
    if tier is None:
        return card
    return replace(card, tier=tier)


def _build_review_queue_cards(
    documents: list[DocumentRow],
    merchants_repo: MerchantRepository,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> list[ReviewQueueCard]:
    """Build one ``ReviewQueueCard`` per document in the manual_review queue.

    Tier is the deal-level score from running ``score_deal`` against
    the merchant's full analyzed-document set — it's identical across
    every card from a single merchant. The cache keyed off
    ``merchant_id`` here ensures a queue with 10 docs from one merchant
    runs score_deal exactly once for that merchant.

    Chunk 3: builds a per-document ``PatternIndex`` from the doc's
    ``AnalysisRow.pattern_analysis`` cache (migration 032) so the chip
    template renders inline ``<details>`` drill-downs of contributing
    source transactions. Docs whose cache is None (legacy pre-chunk-2
    rows) produce an empty index and their chips degrade to plain
    spans — graceful by construction.
    """
    # Pre-fetch analyses in one batched query; transactions are
    # fetched per-doc lazily (only for docs with a populated cache).
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in documents])
    tier_cache: dict[UUID, str | None] = {}
    cards: list[ReviewQueueCard] = []

    for d in documents:
        merchant: MerchantRow | None = None
        merchant_id_str: str | None = None
        merchant_label = "—"
        tier: str | None = None
        if d.merchant_id is not None:
            merchant_id_str = str(d.merchant_id)
            try:
                merchant = merchants_repo.get(d.merchant_id)
                merchant_label = merchant.business_name
            except MerchantNotFoundError:
                merchant_label = f"merchant {str(d.merchant_id)[:8]} (deleted)"
            if merchant is not None:
                if d.merchant_id not in tier_cache:
                    tier_cache[d.merchant_id] = _compute_merchant_tier(merchant, docs, ofac)
                tier = tier_cache[d.merchant_id]

        analysis = analyses_by_doc.get(d.id)
        if analysis is not None and analysis.pattern_analysis is not None:
            transactions = docs.list_transactions(d.id)
        else:
            transactions = []
        pattern_index = PatternIndex.build_for_document(
            analysis=analysis,
            transactions=transactions,
            document_id=d.id,
            filename=d.original_filename,
        )

        raw_flags = list(d.all_flags) if d.all_flags else []
        cards.append(
            ReviewQueueCard(
                document_id=str(d.id),
                filename=d.original_filename,
                uploaded_at=d.uploaded_at.strftime("%Y-%m-%d %H:%M"),
                fraud_score=d.fraud_score,
                fraud_band=derive_fraud_band(d.fraud_score),
                merchant_id=merchant_id_str,
                merchant_label=merchant_label,
                merchant_state=merchant.state if merchant else None,
                merchant_naics=merchant.industry_naics if merchant else None,
                requested_amount=merchant.requested_amount if merchant else None,
                tier=tier,
                flags=categorize_flags(raw_flags, pattern_index=pattern_index),
                # Migration 034 — surface lifecycle status for the
                # status chip. ``None`` for orphan-merchant docs so
                # the template's truthy guard short-circuits cleanly.
                merchant_status=merchant.status if merchant else None,
            )
        )
    return cards


def _format_activity_time(value: object) -> str:
    """Render an audit_log ``created_at`` value for the dashboard timeline."""
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
        except ValueError:
            return value[:16]
    return "—"


@router.get("/review", response_class=HTMLResponse)
async def review_queue(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> HTMLResponse:
    """Manual-review queue — one card per document with parse_status=manual_review.

    Per-document cards share the Today card vocabulary
    (``_humanized_chip.html.j2`` + categorized flags + merchant header)
    so the two surfaces read the same way. The queue is hard-capped at
    ``_REVIEW_QUEUE_DISPLAY_CAP`` documents in the response; if the cap
    is hit the template surfaces a banner so the operator knows the
    queue is deeper than what they see — no silent truncation.
    """
    review_docs = docs.list_documents(parse_status="manual_review", limit=_REVIEW_QUEUE_DISPLAY_CAP)
    cards = _build_review_queue_cards(review_docs, merchants, docs, ofac)
    return templates.TemplateResponse(
        request,
        "review.html.j2",
        {
            "cards": cards,
            "category_labels": CATEGORY_LABELS,
            "queue_capacity": _REVIEW_QUEUE_DISPLAY_CAP,
        },
    )


@router.get("/deals", response_class=HTMLResponse)
async def list_deals(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
) -> HTMLResponse:
    """Deal lifecycle table.

    A "deal" is the derived join (merchant, latest document, latest analysis)
    per the Phase 7 audit decision. There is no ``deals`` table; this view
    enumerates merchants and shows their most recent document's parse status
    and analysis tier proxy. Merchants without any document show as
    ``Awaiting upload``.

    Each row also exposes ``submitted_funder_count`` (distinct funders the
    merchant has been submitted to in the last 90 days) and
    ``submitted_funder_ids`` (the supporting funder_id list per the
    CLAUDE.md auditability rule, so the deals-page "Pending submission"
    filter chip and "Submitted to N funders" column can drill back to the
    contributing submissions). One batched
    ``list_in_window`` query feeds every merchant — never one per row.
    """
    merchants = list(merchants_repo.list_all())

    # Batch fetch the latest document per merchant. Repository returns
    # documents most-recent-first; deduping by merchant_id and keeping
    # the first occurrence yields each merchant's latest. Then one
    # batch analyses fetch covers all of those documents. Total: 2
    # queries regardless of merchant count (was 2N).
    all_docs = docs.list_documents(limit=500)
    latest_by_merchant: dict[UUID, DocumentRow] = {}
    for d in all_docs:
        if d.merchant_id is None or d.merchant_id in latest_by_merchant:
            continue
        latest_by_merchant[d.merchant_id] = d

    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in latest_by_merchant.values()])

    # Per-merchant submission tally, 90-day window — matches the funder-
    # list approval-rate window so the two surfaces agree on "active
    # pipeline". One query, then bucket in Python.
    now = datetime.now(UTC)
    window_start = now - timedelta(days=_DEALS_LIST_SUBMISSION_WINDOW_DAYS)
    submissions = funder_note_subs.list_in_window(from_dt=window_start, to_dt=now)
    distinct_funders_by_merchant: dict[UUID, set[UUID]] = {}
    for s in submissions:
        distinct_funders_by_merchant.setdefault(s.merchant_id, set()).add(s.funder_id)

    rows: list[dict[str, Any]] = []
    for m in merchants:
        latest_doc = latest_by_merchant.get(m.id)
        latest_analysis = analyses_by_doc.get(latest_doc.id) if latest_doc is not None else None
        funder_ids = sorted(distinct_funders_by_merchant.get(m.id, set()))
        rows.append(
            {
                "merchant_id": str(m.id),
                "business_name": m.business_name,
                "state": m.state,
                # Migration 034 — merchant lifecycle status. Surfaced
                # in the Deals table row so the template can render a
                # "provisional" / "needs naming" status chip next to
                # placeholder business_names. Stays a plain string so
                # the existing parse_status chip macro can consume it
                # without any model-class round-trip.
                "status": m.status,
                "uploaded_at": (latest_doc.uploaded_at.strftime("%Y-%m-%d") if latest_doc else "—"),
                "parse_status": latest_doc.parse_status if latest_doc else "no_upload",
                "fraud_score": (
                    latest_doc.fraud_score
                    if latest_doc and latest_doc.fraud_score is not None
                    else "—"
                ),
                "tier_proxy": _tier_proxy(latest_analysis),
                "document_id": str(latest_doc.id) if latest_doc else None,
                "submitted_funder_count": len(funder_ids),
                "submitted_funder_ids": [str(fid) for fid in funder_ids],
            }
        )
    rows.sort(key=lambda r: r["uploaded_at"], reverse=True)
    return templates.TemplateResponse(request, "deals.html.j2", {"rows": rows})


def _tier_proxy(analysis: AnalysisRow | None) -> str:
    """Cheap tier hint for ``/ui/deals`` derived view.

    A real Tier comes from ``score_deal``; the lifecycle table doesn't run
    the scorer for every row (cost + side effects via OFAC). We surface a
    proxy from the parsed-analysis numbers — operators click into the deal
    detail to get the authoritative tier from the scoring API.
    """
    if analysis is None:
        return "—"
    if analysis.num_nsf >= 10 or analysis.days_negative > 15:
        return "F (proxy)"
    if analysis.mca_positions >= 2:
        return "F (proxy)"
    if analysis.num_nsf >= 5 or analysis.days_negative > 5:
        return "D/C (proxy)"
    if analysis.num_nsf >= 2:
        return "B (proxy)"
    return "A/B (proxy)"
