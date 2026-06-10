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
helper itself still lives in ``router.py`` because ~9 still-resident
routes (merchants/{id}, submit, funder-response, dossier.pdf) reference
it and pulling it out would also require pulling the bundling helpers
(``_select_default_bundle`` / ``_filter_to_bundle`` / ``BundleKey``).
``_compute_merchant_tier`` lazy-imports it to avoid the obvious cycle
(``router.py`` imports this module at the top).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_ofac_client,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    MerchantNotFoundError,
    MerchantRepository,
)
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


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
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
    attention = _build_attention_groups(
        attention_docs, merchants_repo, docs, max_groups=8
    )
    # Decorate each card with its deal tier — runs score_deal per
    # merchant. Capped at 8 cards so the cost stays bounded; failures
    # leave tier=None so the queue still renders.
    attention = [
        _enrich_attention_card_with_tier(card, merchants_repo, docs, ofac)
        for card in attention
    ]

    submitted_count = sum(
        1 for r in recent_activity_rows if r.get("action") == "deal.submit_to_funders"
    )
    funded_count = sum(
        1 for r in recent_activity_rows if r.get("action") == "deal.funded"
    )

    funnel_rows = _build_funnel_rows(
        intake_count=merchant_total,
        docs_uploaded=in_pipeline,
        parsed=parsed_total,
        underwritten=proceed + review,
        submitted=submitted_count,
        funded=funded_count,
        declined=manual_review + error,
    )

    return templates.TemplateResponse(
        request,
        "index.html.j2",
        {
            "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
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
    # Pre-fetch all docs' analyses in one batched query. Transactions
    # are fetched lazily below, only for docs whose pattern_analysis is
    # populated, so legacy rows skip the per-doc transactions round-trip.
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in documents])
    transactions_by_doc: dict[UUID, list[ClassifiedTransaction]] = {}

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
                try:
                    merchant = merchants_repo.get(key)
                    label = merchant.business_name
                except MerchantNotFoundError:
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
        scores = [
            doc["fraud_score"]
            for doc in g["documents"]
            if doc["fraud_score"] is not None
        ]
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
                flags=categorize_flags(
                    g["_seen_flags"], pattern_index=pattern_index
                ),
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
    # Lazy import — _collect_analyzed_for_merchant lives in
    # aegis.web.router (still used by ~9 routes there + by the bundling
    # helpers). router.py imports this module at the top, so an eager
    # import the other direction is a cycle.
    from aegis.web.router import _collect_analyzed_for_merchant

    try:
        items = _collect_analyzed_for_merchant(docs, merchant.id, bundle=None)
        if not items:
            return None
        score_input = _score_input_multi_month(merchant, items)
        score_result = score_deal(score_input, ofac=ofac)
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
                    tier_cache[d.merchant_id] = _compute_merchant_tier(
                        merchant, docs, ofac
                    )
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
    review_docs = docs.list_documents(
        parse_status="manual_review", limit=_REVIEW_QUEUE_DISPLAY_CAP
    )
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
) -> HTMLResponse:
    """Deal lifecycle table.

    A "deal" is the derived join (merchant, latest document, latest analysis)
    per the Phase 7 audit decision. There is no ``deals`` table; this view
    enumerates merchants and shows their most recent document's parse status
    and analysis tier proxy. Merchants without any document show as
    ``Awaiting upload``.
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

    analyses_by_doc = docs.get_analyses_by_document_ids(
        [d.id for d in latest_by_merchant.values()]
    )

    rows: list[dict[str, Any]] = []
    for m in merchants:
        latest_doc = latest_by_merchant.get(m.id)
        latest_analysis = (
            analyses_by_doc.get(latest_doc.id) if latest_doc is not None else None
        )
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
                "uploaded_at": (
                    latest_doc.uploaded_at.strftime("%Y-%m-%d") if latest_doc else "—"
                ),
                "parse_status": latest_doc.parse_status if latest_doc else "no_upload",
                "fraud_score": (
                    latest_doc.fraud_score
                    if latest_doc and latest_doc.fraud_score is not None
                    else "—"
                ),
                "tier_proxy": _tier_proxy(latest_analysis),
                "document_id": str(latest_doc.id) if latest_doc else None,
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
