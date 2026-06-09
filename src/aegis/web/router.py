"""Operator dashboard routes.

Five pages + one HTMX partial:

  * ``GET /ui/``                              — index with summary tiles
  * ``GET /ui/upload``                        — upload form (POSTs to /upload)
  * ``GET /ui/merchants``                     — table of all merchants
  * ``GET /ui/merchants/{id}``                — merchant detail with aggregates
  * ``GET /ui/documents/{id}/aggregate/{name}``  — HTMX partial: drill-down
    transactions for one aggregate. Returned as HTML fragment so HTMX
    can swap into the detail page.

Auth note
---------
The dashboard intentionally does NOT require the bearer token: in
production it sits behind Cloudflare Access (SSO + JWT). The bearer
token guards programmatic API endpoints, not the operator UI. In a
local dev box without Cloudflare in front, the dashboard is reachable
on localhost only.
"""

from __future__ import annotations

import io
import urllib.parse
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aegis.api.deps import (
    get_audit,
    get_deal_repository,
    get_decision_snapshot,
    get_funder_reply_repository,
    get_funder_repository,
    get_llm,
    get_merchant_repository,
    get_ofac_client,
    get_override_repository,
    get_renewal_attestation_repository,
    get_repository,
)

# ``aegis.api.routes.upload`` imported lazily inside ``_persist_uploaded_files``
# below to break a package-init cycle: ``aegis.api.routes.__init__`` imports
# ``aegis.web.router`` (to wire the dashboard router), so a module-level
# import the other direction makes ``aegis.web.router`` depend on
# ``aegis.api.routes`` finishing first. With the explicit submodule-attribute
# import in ``aegis.api.routes.__init__`` (post the A.3 router-shadowing fix),
# the eager path raises ``ImportError: cannot import name 'router' from
# partially initialized module 'aegis.web.router'`` on certain pytest
# collection orders. Pre-A.3 the same cycle existed but was silently masked
# by the import binding to the submodule object instead of the APIRouter.
# Lazy import + sys.modules caching → zero per-call overhead.
from aegis.audit import AuditLog
from aegis.close.orchestration import enqueue_close_orchestration
from aegis.compliance.overrides import (
    OverrideError,
    OverridePayload,
    OverrideRepository,
    record_override,
)
from aegis.compliance.snapshot import (
    DecisionSnapshot,
    InMemoryDecisionSnapshot,
)
from aegis.compliance.states import STATES, StateNotServed, validate_state_served
from aegis.config import get_settings
from aegis.deals.portfolio_analytics import (
    DateRange,
    compute_portfolio_metrics,
    resolve_date_range,
)
from aegis.deals.repository import DealRepository
from aegis.funders.extract import (
    FunderExtractionError,
    extract_funder_guidelines,
    extract_funder_guidelines_from_image,
    merge_extractions,
)
from aegis.funders.models import FunderRow, FunderTier
from aegis.funders.replies import FunderReplyRepository
from aegis.funders.repository import (
    FunderNotFoundError,
    FunderRepository,
)
from aegis.llm import LLMClient
from aegis.merchants.models import EntityType, MerchantRow
from aegis.merchants.renewal_attestations import (
    RenewalAttestationConflictError,
    RenewalAttestationRepository,
    RenewalAttestationWriteError,
    record_renewal_attestation,
)
from aegis.merchants.repository import (
    MerchantConflictError,
    MerchantNotFoundError,
    MerchantRepository,
    list_upcoming_renewals,
)
from aegis.ops.operators import resolve_operator_email
from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import (
    PatternAnalysis,
    analyze_patterns,
    pattern_analysis_from_dto,
)
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import FunderMatch, ScoreInput
from aegis.scoring.multi_month import (
    detect_missing_months as _detect_missing_months,
)
from aegis.scoring.multi_month import (
    score_input_multi_month as _score_input_multi_month,
)
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
from aegis.scoring.submission_package import build_submission_files
from aegis.storage import (
    AnalysisRow,
    DocumentNotFoundError,
    DocumentRepository,
    DocumentRow,
)
from aegis.web._attention_card import (
    CATEGORY_LABELS,
    AttentionCard,
    DocumentPatternContext,
    PatternIndex,
    ReviewQueueCard,
    categorize_flags,
    derive_fraud_band,
)
from aegis.web._flag_labels import humanize_audit_action, humanize_flag
from aegis.web._pattern_cards import (
    build_pattern_cards,
    humanize_hard_decline,
    humanize_soft_concern,
    pattern_has_customer_concentration,
)
from aegis.web._slug import slugify
from aegis.web._soft_signals import parse_soft_signal_flags
from aegis.web._stacking_card import build_stacking_card

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Jinja filters accept arbitrary template-side values (None, Decimal, int,
# str). The unions below cover what AEGIS actually sends through — typing
# more narrowly would force callers to pre-coerce, defeating the filter's
# purpose. Justifies the broad input types per CLAUDE.md "Any" rule.
_MoneyLike = Decimal | int | float | str | None
_NumericLike = int | str | None


def _money_filter(value: _MoneyLike, *, whole: bool = False) -> str:
    """Format a Decimal/int/float as $X,XXX[.XX]. None → em-dash."""
    if value is None or value == "":
        return "—"
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value)
    sign = "-" if d < 0 else ""
    d = abs(d)
    if whole or d == d.to_integral_value():
        whole_part = int(d)
        return f"{sign}${whole_part:,}"
    cents = d.quantize(Decimal("0.01"))
    int_part, _, frac = str(cents).partition(".")
    return f"{sign}${int(int_part):,}.{frac}"


def _whole_money_filter(value: _MoneyLike) -> str:
    return _money_filter(value, whole=True)


def _format_pct_filter(value: _MoneyLike) -> str:
    """Render a Decimal fraction (0.365) as a percent (``36.5%``).

    Returns ``"unavailable"`` for ``None`` rather than ``0.00%`` — per
    the R0.4 regulator-grade-lie discipline: a 0% APR rendered next to
    a 1.30x factor is the lie we explicitly refuse to render.
    Estimated-terms APR may be ``None`` when the IRR optimizer cannot
    bracket a root; surfacing that as 0% would manufacture false
    precision the operator could then quote to a funder rep.
    """
    if value is None:
        return "unavailable"
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return str(value)
    pct = (d * Decimal("100")).quantize(Decimal("0.01"))
    int_part, _, frac = str(pct).partition(".")
    if not frac:
        return f"{int_part}%"
    return f"{int_part}.{frac}%"


def _days_label_filter(value: _NumericLike) -> str:
    if value is None or value == "":
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{n} day" if n == 1 else f"{n} days"


def _fraud_band(score: _NumericLike) -> str:
    """Map fraud_score 0-100 to a risk band keyed off pipeline.py thresholds.

    Bands mirror parser.pipeline constants exactly: REVIEW_THRESHOLD=35,
    HARD_DECLINE_THRESHOLD=65. Keeps UI legend in sync with parse_status gate.
    """
    if score is None:
        return "unknown"
    try:
        n = int(score)
    except (TypeError, ValueError):
        return "unknown"
    if n < 35:
        return "clear"
    if n < 65:
        return "review"
    return "decline"


templates.env.filters["money"] = _money_filter
templates.env.filters["whole_money"] = _whole_money_filter
templates.env.filters["format_pct"] = _format_pct_filter
templates.env.filters["days_label"] = _days_label_filter
templates.env.filters["fraud_band"] = _fraud_band
templates.env.filters["humanize_flag"] = humanize_flag
# Verdict-section humanizers — added in the dossier signal-legibility
# consolidation (v2 catalog Bucket B; ``_pattern_cards.py``). Used by
# merchant_detail_dossier.html.j2 to render the hard-decline and
# soft-concern lists as worker-language sentences instead of raw
# identifier strings. Pattern cards already render through their own
# PATTERN_COPY map; these two close the gap for the verdict section.
templates.env.filters["humanize_hard_decline"] = humanize_hard_decline
templates.env.filters["humanize_soft_concern"] = humanize_soft_concern

router = APIRouter(prefix="/ui", tags=["dashboard"])


_AGGREGATE_LABELS: dict[str, str] = {
    "true_revenue": "True Revenue",
    "avg_daily_balance": "Average Daily Balance",
    "num_nsf": "NSF Count",
    "days_negative": "Days Negative",
    "mca_daily_total": "MCA Daily Total",
}

# Per-aggregate unit hint shown under the KPI value (e.g. "$" amount,
# "days", "count"). Kept aligned with _AGGREGATE_LABELS — every key
# present in labels must have an entry here so the KPI tile can format.
_AGGREGATE_UNIT_KIND: dict[str, str] = {
    "true_revenue": "money",
    "avg_daily_balance": "money",
    "num_nsf": "count",
    "days_negative": "days",
    "mca_daily_total": "money",
}

_AGGREGATE_SOURCE_FIELDS: dict[str, str] = {
    "true_revenue": "true_revenue_source_ids",
    "avg_daily_balance": "avg_daily_balance_source_ids",
    "num_nsf": "num_nsf_source_ids",
    "days_negative": "days_negative_source_ids",
    "mca_daily_total": "mca_daily_total_source_ids",
}


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


# Audit action humanization moved to ``aegis.web._flag_labels``. The
# Proposal 1 inline helper handled the bare submit action; Proposal 2's
# ``humanize_audit_action`` extends it with funder names pulled from the
# audit row's ``details`` field, so the feed reads "recorded submission
# to OnDeck, Credibly" instead of "recorded submission to funders".


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "upload.html.j2",
        {
            "merchants": merchants_repo.list_all(),
            "results": None,
            "error": None,
            "merchant_just_uploaded": None,
        },
    )


@router.post("/upload", response_class=HTMLResponse, response_model=None)
async def upload_submit(
    request: Request,
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    merchant_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Browser-friendly multi-file upload — no bearer (Cloudflare Access in prod).

    Streams up to N PDFs in one multipart request, hashes/dedups each via
    the shared ``persist_pdf_upload`` helper, and renders an inline
    summary with each file's status. ``merchant_id`` is optional from
    this route — operators uploading ad-hoc may not have the merchant
    record yet (see ``/ui/intake`` for the combined create + upload flow).

    ``files`` is typed as ``| None`` so a browser submit with no file
    selected falls into the friendly HTML error branch below instead of
    bouncing off FastAPI's 422 validation gate as opaque JSON.
    """
    if not files or all(not f.filename for f in files):
        return templates.TemplateResponse(
            request,
            "upload.html.j2",
            {
                "merchants": merchants_repo.list_all(),
                "results": None,
                "error": "No files provided.",
                "merchant_just_uploaded": None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    parsed_merchant_id: UUID | None = None
    if merchant_id.strip():
        try:
            parsed_merchant_id = UUID(merchant_id.strip())
            merchants_repo.get(parsed_merchant_id)
        except (ValueError, MerchantNotFoundError):
            return templates.TemplateResponse(
                request,
                "upload.html.j2",
                {
                    "merchants": merchants_repo.list_all(),
                    "results": None,
                    "error": f"Unknown merchant_id {merchant_id!r}.",
                    "merchant_just_uploaded": None,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    else:
        # Migration 034 — auto-create branch (chunk B).
        #
        # The operator dropped files without picking a merchant. Create
        # ONE provisional merchant for the batch, attach every file to
        # it. Worker finalize at parse-completion fills the name from
        # ``statement.account_holder``; the failure paths flag the
        # merchant for manual naming so nothing zombies in
        # ``provisional`` forever.
        #
        # Scope (locked): this branch lives ONLY on the dashboard
        # ``/ui/upload``. The bearer ``/upload``, the Close-attachment
        # ``/uploads/from-close``, and the operator-curated
        # ``/ui/intake`` all keep their existing behavior (orphan,
        # Close-lead-resolved, and manual-create respectively).
        valid_files = [f for f in files if f.filename]
        if valid_files:
            provisional = merchants_repo.create_provisional()
            parsed_merchant_id = provisional.id
            audit.record(
                actor="dashboard",
                actor_email=actor_email,
                action="merchant.provisional_created",
                subject_type="merchant",
                subject_id=provisional.id,
                details={
                    "batch_size": len(valid_files),
                    "file_names": [f.filename for f in valid_files],
                    "uploaded_by": actor_email or "dashboard",
                },
            )

    settings = get_settings()
    results, total_error = await _persist_uploads(
        request=request,
        files=files,
        repository=repository,
        audit=audit,
        actor="dashboard",
        actor_email=actor_email,
        merchant_id=parsed_merchant_id,
        per_file_cap=settings.aegis_max_upload_bytes,
        total_cap=settings.aegis_max_intake_total_bytes,
    )

    # When at least one file landed on a specific merchant, surface that
    # merchant on the completion card so the broker can jump straight to
    # detail / match-funders without re-typing.
    merchant_just_uploaded: MerchantRow | None = None
    if parsed_merchant_id is not None and any(
        r.status in {"ok", "duplicate"} for r in results
    ):
        try:
            merchant_just_uploaded = merchants_repo.get(parsed_merchant_id)
        except MerchantNotFoundError:
            merchant_just_uploaded = None

    return templates.TemplateResponse(
        request,
        "upload.html.j2",
        {
            "merchants": merchants_repo.list_all(),
            "results": results,
            "error": total_error,
            "merchant_just_uploaded": merchant_just_uploaded,
        },
        status_code=(
            status.HTTP_200_OK if not total_error else status.HTTP_400_BAD_REQUEST
        ),
    )


@router.get("/intake", response_class=HTMLResponse)
async def intake_form(request: Request) -> HTMLResponse:
    """Combined intake: create merchant + upload N statements in one POST."""
    return templates.TemplateResponse(
        request, "intake.html.j2", {"error": None, "form": {}}
    )


@router.post("/intake", response_class=HTMLResponse, response_model=None)
async def intake_submit(
    request: Request,
    repository: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    business_name: Annotated[str, Form()],
    owner_name: Annotated[str, Form()],
    state: Annotated[str, Form()],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    dba: Annotated[str, Form()] = "",
    industry_naics: Annotated[str, Form()] = "",
    credit_score: Annotated[str, Form()] = "",
    time_in_business_months: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    entity_type: Annotated[str, Form()] = "",
    ein: Annotated[str, Form()] = "",
    requested_amount: Annotated[str, Form()] = "",
    requested_factor: Annotated[str, Form()] = "",
    requested_term_days: Annotated[str, Form()] = "",
    broker_source: Annotated[str, Form()] = "",
    intake_date: Annotated[str, Form()] = "",
    is_renewal: Annotated[str, Form()] = "false",
) -> HTMLResponse | RedirectResponse:
    """Create merchant + upload N statements atomically.

    On any validation error the merchant is NOT created and the form
    re-renders with the entered values preserved. On success the
    operator lands on the merchant findings page with all uploaded
    documents already persisted.
    """
    form_payload: dict[str, Any] = {
        "business_name": business_name,
        "owner_name": owner_name,
        "state": state,
        "dba": dba,
        "industry_naics": industry_naics,
        "credit_score": credit_score,
        "time_in_business_months": time_in_business_months,
        "email": email,
        "phone": phone,
        "entity_type": entity_type,
        "ein": ein,
        "requested_amount": requested_amount,
        "requested_factor": requested_factor,
        "requested_term_days": requested_term_days,
        "broker_source": broker_source,
        "intake_date": intake_date,
        "is_renewal": is_renewal,
    }

    state_err = _validate_merchant_state(state)
    if state_err is not None:
        return _intake_form_error(request, state_err, form_payload)

    try:
        merchant = MerchantRow(
            business_name=business_name,
            owner_name=owner_name,
            state=state.upper(),
            dba=dba or None,
            industry_naics=industry_naics or None,
            credit_score=int(credit_score) if credit_score else None,
            time_in_business_months=int(time_in_business_months)
            if time_in_business_months
            else None,
            email=email or None,
            phone=phone or None,
            entity_type=_entity_type_or_none(entity_type),
            ein=ein or None,
            requested_amount=Decimal(requested_amount) if requested_amount else None,
            requested_factor=Decimal(requested_factor) if requested_factor else None,
            requested_term_days=int(requested_term_days) if requested_term_days else None,
            broker_source=broker_source or None,
            intake_date=date.fromisoformat(intake_date) if intake_date else None,
            is_renewal=is_renewal.lower() in {"true", "on", "yes", "1"},
        )
    except (ValueError, TypeError) as exc:
        return _intake_form_error(request, str(exc), form_payload)
    try:
        merchant = merchants_repo.upsert(merchant)
    except MerchantConflictError as exc:
        return _intake_form_error(request, str(exc), form_payload)

    # Files are optional at intake — operator can create merchant first
    # and upload later.
    docs_uploaded = 0
    docs_failed = 0
    valid_files = [f for f in (files or []) if f.filename]
    if valid_files:
        settings = get_settings()
        results, total_error = await _persist_uploads(
            request=request,
            files=valid_files,
            repository=repository,
            audit=audit,
            actor="dashboard",
            actor_email=actor_email,
            merchant_id=merchant.id,
            per_file_cap=settings.aegis_max_upload_bytes,
            total_cap=settings.aegis_max_intake_total_bytes,
        )
        docs_uploaded = sum(1 for r in results if r.status in {"ok", "duplicate"})
        docs_failed = sum(1 for r in results if r.status == "error")
        if total_error:
            # Merchant created but uploads failed; surface the error and
            # redirect to merchant detail so operator can retry uploads
            # individually from /ui/upload.
            audit.record(
                actor="dashboard",
                actor_email=actor_email,
                action="intake.partial_failure",
                subject_type="merchant",
                subject_id=merchant.id,
                details={"error": total_error, "results_count": len(results)},
            )

    # Carry a "just created" flash to the merchant detail page so it can
    # render a confirmation banner with upload count + next-step CTAs.
    target = (
        f"/ui/merchants/{merchant.id}"
        f"?from_intake=1&docs={docs_uploaded}"
        f"{'&failed=' + str(docs_failed) if docs_failed else ''}"
    )
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/merchants", response_class=HTMLResponse)
async def list_merchants(
    request: Request,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "merchants.html.j2", {"merchants": repo.list_all()}
    )


_REVIEW_QUEUE_DISPLAY_CAP: Final[int] = 200


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


# Close-queue thresholds. A pull enqueued but not completed within this
# many hours is suspect — the worker either crashed silently or the lead
# has a payload Close hasn't published yet; either way it surfaces as
# STUCK so the operator can retry. Parsing-pending threshold is much
# tighter because a Bedrock parse should not take more than a few
# minutes per document.
_CLOSE_QUEUE_STALE_PULL_HOURS: Final[float] = 6.0
_CLOSE_QUEUE_STALE_PARSE_HOURS: Final[float] = 1.0


# Flag-category → human label for the GATED detail line. The classifier
# peeks at all_flags on each manual_review doc and surfaces the unique
# categories so the operator sees WHY at a glance — "editor metadata +
# reconciliation drift" reads as a tampering signal, "OFAC match" as a
# sanctions hit, "OCR concerns" as a missing-data review. Distinct
# semantics demand distinct response — re-pulling won't change
# tampering flags but it might clear an OCR concern.
_CLOSE_QUEUE_FLAG_CATEGORY_LABELS: Final[dict[str, str]] = {
    "META":    "editor metadata",
    "MATH":    "reconciliation drift",
    "PATTERN": "pattern signal",
    "STRUCT":  "PDF structure",
    "OFAC":    "OFAC match",
    "LLM":     "LLM concerns",
    "OCR":     "OCR concerns",
}
_CLOSE_QUEUE_FLAG_CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "OFAC", "META", "MATH", "PATTERN", "STRUCT", "OCR", "LLM",
)


def _gating_reason_labels(docs: list[DocumentRow]) -> list[str]:
    """Extract unique [CATEGORY] flag prefixes across docs, map to
    human labels in stable order. Empty list if no docs carry tagged
    flags — fall back to a generic phrase in the classifier."""
    found: set[str] = set()
    for d in docs:
        for f in (d.all_flags or []):
            if isinstance(f, str) and f.startswith("[") and "]" in f:
                cat = f[1:f.find("]")]
                if cat in _CLOSE_QUEUE_FLAG_CATEGORY_LABELS:
                    found.add(cat)
    return [
        _CLOSE_QUEUE_FLAG_CATEGORY_LABELS[c]
        for c in _CLOSE_QUEUE_FLAG_CATEGORY_ORDER
        if c in found
    ]


def _parse_audit_ts(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _hours_since(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (now - ts).total_seconds() / 3600.0)


def _classify_close_pipeline_state(
    *,
    docs: list[DocumentRow],
    audit_rows: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Derive the Close-queue state for one merchant.

    Returns a dict with ``state`` (machine token), ``label`` (chip
    text), ``severity`` (chip color: good/warn/bad/info), ``action``
    (``retry`` / ``review`` / ``None``), and ``detail`` (one-line
    human reason).

    States distinguish three operator-relevant categories:

    * **Needs a retry** — ``failed_pull``, ``failed_parse``, ``stuck``.
      The rescan button is the right action.
    * **Needs an underwriter** — ``gated``. The parser ran and flagged
      integrity / reconciliation concerns; the right action is to
      open the dossier, not to retry.
    * **Informational** — ``awaiting_pull``, ``parsing``, ``scored``.
      No action needed.

    At 30 deals/day the distinction matters: "Score unavailable" on
    the dossier is ambiguous (broken pipeline vs. flagged for human
    review vs. still working). The queue makes the reason readable
    at a glance.
    """
    # Defensive: sort by created_at descending so we look at the LATEST
    # orchestration outcome regardless of how the caller ordered the
    # rows. The audit-log API returns newest-first today, but a future
    # bulk-load path could pass them oldest-first and silently invert
    # the verdict (e.g. "enqueued" stays current after "list_failed").
    close_orch_rows = sorted(
        (
            r
            for r in audit_rows
            if str(r.get("action", "")).startswith("close.orchestration.")
        ),
        key=lambda r: _parse_audit_ts(r.get("created_at")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    last_orch = close_orch_rows[0] if close_orch_rows else None
    last_action = (last_orch or {}).get("action")
    last_ts = _parse_audit_ts((last_orch or {}).get("created_at"))

    if not docs:
        if last_action == "close.orchestration.list_failed":
            details = (last_orch or {}).get("details") or {}
            err_msg = ""
            if isinstance(details, dict):
                err_msg = str(details.get("message") or details.get("error") or "")
            return {
                "state": "failed_pull",
                "label": "Failed to pull",
                "severity": "bad",
                "action": "retry",
                "detail": (
                    f"Close listing failed: {err_msg[:80]}"
                    if err_msg
                    else "Close listing failed"
                ),
            }
        if last_action in (
            "close.orchestration.enqueued",
            "close.orchestration.manual_rescan",
        ):
            elapsed_h = _hours_since(last_ts, now)
            if elapsed_h is not None and elapsed_h > _CLOSE_QUEUE_STALE_PULL_HOURS:
                return {
                    "state": "stuck",
                    "label": f"Stuck (no pull, {elapsed_h:.0f}h)",
                    "severity": "warn",
                    "action": "retry",
                    "detail": (
                        f"Pull enqueued {elapsed_h:.0f}h ago with no completion"
                    ),
                }
            return {
                "state": "awaiting_pull",
                "label": "Pulling",
                "severity": "info",
                "action": None,
                "detail": "Close attachment pull in flight",
            }
        return {
            "state": "stuck",
            "label": "Stuck (no audit)",
            "severity": "warn",
            "action": "retry",
            "detail": "No Close orchestration audit on file",
        }

    pending = [d for d in docs if d.parse_status == "pending"]
    error = [d for d in docs if d.parse_status == "error"]
    manual_review = [d for d in docs if d.parse_status == "manual_review"]
    clean = [d for d in docs if d.parse_status in ("proceed", "review")]

    if pending:
        oldest_ts = min(
            (d.uploaded_at for d in pending if d.uploaded_at is not None),
            default=None,
        )
        elapsed_h = _hours_since(oldest_ts, now)
        if elapsed_h is not None and elapsed_h > _CLOSE_QUEUE_STALE_PARSE_HOURS:
            return {
                "state": "stuck",
                "label": f"Stuck (parse {elapsed_h:.0f}h)",
                "severity": "warn",
                "action": "retry",
                "detail": (
                    f"{len(pending)} document(s) pending parse for "
                    f"{elapsed_h:.0f}h"
                ),
            }
        return {
            "state": "parsing",
            "label": f"Parsing {len(docs) - len(pending)}/{len(docs)}",
            "severity": "info",
            "action": None,
            "detail": f"{len(pending)} document(s) still parsing",
        }

    if manual_review:
        extras = []
        if error:
            extras.append(f"{len(error)} errored")
        if clean:
            extras.append(f"{len(clean)} clean")
        suffix = (" · " + ", ".join(extras)) if extras else ""
        # Surface the gating reasons (flag categories) so the operator
        # can distinguish tampering (editor metadata + reconciliation
        # drift) from OFAC, from OCR concerns, from PDF structure issues
        # — each implies a different next move.
        reason_labels = _gating_reason_labels(manual_review)
        reason_phrase = (
            " + ".join(reason_labels)
            if reason_labels
            else "integrity / reconciliation concerns"
        )
        return {
            "state": "gated",
            "label": "Needs underwriter",
            "severity": "warn",
            "action": "review",
            "detail": (
                f"{len(manual_review)} statement(s) flagged · "
                f"{reason_phrase}{suffix}"
            ),
        }
    if error and not clean:
        return {
            "state": "failed_parse",
            "label": "Failed to parse",
            "severity": "bad",
            "action": "retry",
            "detail": f"All {len(error)} document(s) errored during parse",
        }
    if clean:
        suffix = f" · {len(error)} errored" if error else ""
        return {
            "state": "scored",
            "label": "Scored",
            "severity": "good",
            "action": None,
            "detail": f"{len(clean)} clean statement(s){suffix}",
        }
    return {
        "state": "stuck",
        "label": "Stuck",
        "severity": "warn",
        "action": "retry",
        "detail": "Unknown document state",
    }


# Sort order: failures first (most urgent), then stuck, then gated
# (operator action needed), then in-flight, then scored. Within a state
# tier, sort alphabetically by business name for predictable scanning.
_CLOSE_QUEUE_STATE_ORDER: Final[dict[str, int]] = {
    "failed_pull":   0,
    "failed_parse":  1,
    "stuck":         2,
    "gated":         3,
    "parsing":       4,
    "awaiting_pull": 5,
    "scored":        6,
}


@router.get("/close-queue", response_class=HTMLResponse)
async def close_queue(
    request: Request,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Pipeline state for every Close-sourced merchant.

    Aggregates merchants where ``close_lead_id IS NOT NULL`` into one
    row each, classified by audit + document state. FAILED rows expose
    the rescan retry button. GATED rows link to the dossier for
    operator review. The point at 30 deals/day is that a silently-stuck
    merchant cannot fall through the cracks — every Close-sourced
    deal surfaces here in a single sortable view with the reason it
    needs (or does not need) attention.
    """
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for m in merchants.list_all():
        if not m.close_lead_id:
            continue
        merchant_docs = docs.list_documents(merchant_id=m.id, limit=50)
        merchant_audit = audit.list_for_subject(
            subject_type="merchant", subject_id=m.id, limit=50
        )
        state = _classify_close_pipeline_state(
            docs=merchant_docs, audit_rows=merchant_audit, now=now
        )
        last_orch = next(
            (
                r
                for r in merchant_audit
                if str(r.get("action", "")).startswith("close.orchestration.")
            ),
            None,
        )
        rows.append(
            {
                "merchant_id": str(m.id),
                "business_name": m.business_name,
                "close_lead_id": m.close_lead_id,
                "state": state,
                "doc_count": len(merchant_docs),
                "last_audit_action": (last_orch or {}).get("action") or "—",
                "last_audit_at": _parse_audit_ts(
                    (last_orch or {}).get("created_at")
                ),
            }
        )
    rows.sort(
        key=lambda r: (
            _CLOSE_QUEUE_STATE_ORDER.get(r["state"]["state"], 99),
            r["business_name"].lower(),
        )
    )
    # State counts for the deck header — "3 failed, 2 needs review, …"
    state_counts: dict[str, int] = {}
    for r in rows:
        state_counts[r["state"]["state"]] = (
            state_counts.get(r["state"]["state"], 0) + 1
        )
    return templates.TemplateResponse(
        request,
        "close_queue.html.j2",
        {
            "rows": rows,
            "state_counts": state_counts,
            "stale_pull_hours": _CLOSE_QUEUE_STALE_PULL_HOURS,
            "stale_parse_hours": _CLOSE_QUEUE_STALE_PARSE_HOURS,
        },
    )


@router.get("/merchants/new", response_class=HTMLResponse)
async def merchant_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "merchant_form.html.j2", {"merchant": None, "error": None}
    )


@router.post("/merchants/new", response_class=HTMLResponse, response_model=None)
async def merchant_new_submit(
    request: Request,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    business_name: Annotated[str, Form()],
    owner_name: Annotated[str, Form()],
    state: Annotated[str, Form()],
    dba: Annotated[str, Form()] = "",
    industry_naics: Annotated[str, Form()] = "",
    credit_score: Annotated[str, Form()] = "",
    time_in_business_months: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    entity_type: Annotated[str, Form()] = "",
    ein: Annotated[str, Form()] = "",
    requested_amount: Annotated[str, Form()] = "",
    requested_factor: Annotated[str, Form()] = "",
    requested_term_days: Annotated[str, Form()] = "",
    broker_source: Annotated[str, Form()] = "",
    intake_date: Annotated[str, Form()] = "",
    is_renewal: Annotated[str, Form()] = "false",
) -> HTMLResponse | RedirectResponse:
    error = _validate_merchant_state(state)
    if error is not None:
        return _merchant_form_error(request, error, _form_dict_from_locals(locals()))
    try:
        row = MerchantRow(
            business_name=business_name,
            owner_name=owner_name,
            state=state.upper(),
            dba=dba or None,
            industry_naics=industry_naics or None,
            credit_score=int(credit_score) if credit_score else None,
            time_in_business_months=int(time_in_business_months)
            if time_in_business_months
            else None,
            email=email or None,
            phone=phone or None,
            entity_type=_entity_type_or_none(entity_type),
            ein=ein or None,
            requested_amount=Decimal(requested_amount) if requested_amount else None,
            requested_factor=Decimal(requested_factor) if requested_factor else None,
            requested_term_days=int(requested_term_days) if requested_term_days else None,
            broker_source=broker_source or None,
            intake_date=date.fromisoformat(intake_date) if intake_date else None,
            is_renewal=is_renewal.lower() in {"true", "on", "yes", "1"},
        )
    except (ValueError, TypeError) as exc:
        return _merchant_form_error(request, str(exc), _form_dict_from_locals(locals()))
    try:
        saved = repo.upsert(row)
    except MerchantConflictError as exc:
        return _merchant_form_error(request, str(exc), _form_dict_from_locals(locals()))
    return RedirectResponse(f"/ui/merchants/{saved.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/merchants/{merchant_id}/edit", response_class=HTMLResponse)
async def merchant_edit_form(
    request: Request,
    merchant_id: UUID,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    try:
        merchant = repo.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request, "merchant_form.html.j2", {"merchant": merchant, "error": None}
    )


@router.post("/merchants/{merchant_id}/edit", response_class=HTMLResponse, response_model=None)
async def merchant_edit_submit(
    request: Request,
    merchant_id: UUID,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    business_name: Annotated[str, Form()],
    owner_name: Annotated[str, Form()],
    state: Annotated[str, Form()],
    dba: Annotated[str, Form()] = "",
    industry_naics: Annotated[str, Form()] = "",
    credit_score: Annotated[str, Form()] = "",
    time_in_business_months: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    entity_type: Annotated[str, Form()] = "",
    ein: Annotated[str, Form()] = "",
    requested_amount: Annotated[str, Form()] = "",
    requested_factor: Annotated[str, Form()] = "",
    requested_term_days: Annotated[str, Form()] = "",
    broker_source: Annotated[str, Form()] = "",
    intake_date: Annotated[str, Form()] = "",
    is_renewal: Annotated[str, Form()] = "false",
) -> HTMLResponse | RedirectResponse:
    try:
        existing = repo.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    error = _validate_merchant_state(state)
    if error is not None:
        return _merchant_form_error(
            request, error, _form_dict_from_locals(locals()), merchant=existing
        )
    try:
        updated = existing.model_copy(
            update={
                "business_name": business_name,
                "owner_name": owner_name,
                "state": state.upper(),
                "dba": dba or None,
                "industry_naics": industry_naics or None,
                "credit_score": int(credit_score) if credit_score else None,
                "time_in_business_months": int(time_in_business_months)
                if time_in_business_months
                else None,
                "email": email or None,
                "phone": phone or None,
                "entity_type": _entity_type_or_none(entity_type),
                "ein": ein or None,
                "requested_amount": Decimal(requested_amount) if requested_amount else None,
                "requested_factor": Decimal(requested_factor) if requested_factor else None,
                "requested_term_days": int(requested_term_days) if requested_term_days else None,
                "broker_source": broker_source or None,
                "intake_date": date.fromisoformat(intake_date) if intake_date else None,
                "is_renewal": is_renewal.lower() in {"true", "on", "yes", "1"},
            }
        )
    except (ValueError, TypeError) as exc:
        return _merchant_form_error(
            request, str(exc), _form_dict_from_locals(locals()), merchant=existing
        )
    try:
        saved = repo.upsert(updated)
    except MerchantConflictError as exc:
        return _merchant_form_error(
            request, str(exc), _form_dict_from_locals(locals()), merchant=existing
        )
    return RedirectResponse(f"/ui/merchants/{saved.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/funders", response_class=HTMLResponse)
async def list_funders_page(
    request: Request,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "funders.html.j2", {"funders": repo.list_active()}
    )


@router.get("/funders/import", response_class=HTMLResponse)
async def funder_import_form(request: Request) -> HTMLResponse:
    """Phase 7B: upload form for funder-criteria PDFs."""
    return templates.TemplateResponse(
        request, "funder_import.html.j2", {"error": None}
    )


_MAX_FUNDER_IMPORT_BYTES = 25 * 1024 * 1024

# Media types accepted at /ui/funders/import. PDFs route through the
# document block; PNG/JPEG route through the image block. Anything else
# is rejected with a 415-like 400 (FastAPI surfaces 415 awkwardly on
# multipart uploads, so we re-render the form with a clear error).
_FUNDER_IMPORT_PDF_TYPES: Final[frozenset[str]] = frozenset({"application/pdf"})
_FUNDER_IMPORT_IMAGE_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/jpg"}
)


def _classify_funder_import_media(
    upload: UploadFile,
) -> str:
    """Return "pdf" or "image" based on Content-Type, falling back to filename.

    Returns "" if neither classification fits — caller renders an error.
    """
    raw = (upload.content_type or "").strip().lower()
    if raw in _FUNDER_IMPORT_PDF_TYPES:
        return "pdf"
    if raw in _FUNDER_IMPORT_IMAGE_TYPES:
        return "image"
    # Filename fallback: browsers occasionally send a generic
    # `application/octet-stream` for drag-dropped images.
    fn = (upload.filename or "").lower()
    if fn.endswith(".pdf"):
        return "pdf"
    if fn.endswith((".png", ".jpg", ".jpeg")):
        return "image"
    return ""


@router.post("/funders/import", response_class=HTMLResponse, response_model=None)
async def funder_import_review(
    request: Request,
    llm: Annotated[LLMClient, Depends(get_llm)],
    pdf: Annotated[list[UploadFile], File()],
) -> HTMLResponse:
    """Run the LLM extraction pass(es) and render an editable review page.

    Accepts one or more files (PDFs and/or PNG/JPEG screenshots). Each
    file is routed by media type — PDFs through the document block,
    images through the vision block — and the per-doc extractions are
    field-merged so the operator sees a single review form.

    The form parameter is still named `pdf` for backward compatibility
    with existing bookmarks / scripts that target this endpoint; it now
    accepts multiple files via the `multiple` attribute on the file
    input.

    Stateless: the rendered form carries every field of the merged draft
    so the save endpoint receives the (possibly edited) values directly.
    Avoids a "drafts" table for Phase 7B.
    """
    # Treat the empty / single-empty-upload case identically.
    uploads = [u for u in pdf if u and (u.filename or u.content_type)]
    if not uploads:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": "no files uploaded"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    extractions: list[Any] = []  # FunderGuidelineExtraction — Any to avoid name
    # collision with the per-file try/except scope.

    for upload in uploads:
        kind = _classify_funder_import_media(upload)
        if kind == "":
            return templates.TemplateResponse(
                request,
                "funder_import.html.j2",
                {
                    "error": (
                        f"unsupported file type for {upload.filename or 'upload'!r}: "
                        f"got {upload.content_type or 'unknown'}. "
                        "Accepted: application/pdf, image/png, image/jpeg."
                    ),
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        body = await upload.read(_MAX_FUNDER_IMPORT_BYTES + 1)
        if len(body) > _MAX_FUNDER_IMPORT_BYTES:
            return templates.TemplateResponse(
                request,
                "funder_import.html.j2",
                {
                    "error": (
                        f"{upload.filename or 'upload'} exceeds "
                        f"{_MAX_FUNDER_IMPORT_BYTES} bytes"
                    ),
                },
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if not body:
            return templates.TemplateResponse(
                request,
                "funder_import.html.j2",
                {"error": f"{upload.filename or 'upload'} was empty"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            if kind == "pdf":
                extraction = extract_funder_guidelines(body, llm)
            else:
                extraction = extract_funder_guidelines_from_image(body, llm)
        except FunderExtractionError as exc:
            return templates.TemplateResponse(
                request,
                "funder_import.html.j2",
                {
                    "error": (
                        f"extraction failed for {upload.filename or 'upload'}: {exc}"
                    ),
                },
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        extractions.append(extraction)

    try:
        merged = merge_extractions(extractions)
    except FunderExtractionError as exc:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": f"merge failed: {exc}"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    # Serialise tiers route-side so the hidden form input survives
    # Decimal precision through JSON round-trip on submit.
    import json as _json

    tiers_json = _json.dumps(
        [t.model_dump(mode="json") for t in merged.draft.tiers]
    )
    return templates.TemplateResponse(
        request,
        "funder_review.html.j2",
        {
            "extraction": merged,
            "low_confidence_threshold": 60,
            "form_errors": [],
            "tiers_json": tiers_json,
        },
    )


@router.post("/funders/import/save", response_class=HTMLResponse, response_model=None)
async def funder_import_save(
    request: Request,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    name: Annotated[str, Form()],
    accepts_stacking: Annotated[str, Form()] = "false",
    min_monthly_revenue: Annotated[str, Form()] = "",
    min_avg_daily_balance: Annotated[str, Form()] = "",
    min_credit_score: Annotated[str, Form()] = "",
    min_months_in_business: Annotated[str, Form()] = "",
    max_positions: Annotated[str, Form()] = "",
    min_advance: Annotated[str, Form()] = "",
    max_advance: Annotated[str, Form()] = "",
    max_nsf_tolerance: Annotated[str, Form()] = "",
    typical_factor_low: Annotated[str, Form()] = "",
    typical_factor_high: Annotated[str, Form()] = "",
    typical_holdback_low: Annotated[str, Form()] = "",
    typical_holdback_high: Annotated[str, Form()] = "",
    excluded_industries: Annotated[str, Form()] = "",
    excluded_states: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    # Step C fields (Finding 1 fold-in for step F):
    contact_name: Annotated[str, Form()] = "",
    contact_phone: Annotated[str, Form()] = "",
    contact_email: Annotated[str, Form()] = "",
    submission_email: Annotated[str, Form()] = "",
    notes_residual: Annotated[str, Form()] = "",
    auto_decline_conditions: Annotated[str, Form()] = "",
    conditional_requirements: Annotated[str, Form()] = "",
    # Tiers travel as a JSON string in a hidden input — operator can't
    # edit individual tier fields in this form (rich tier editing is a
    # separate feature). On a fresh import the extraction's tier list
    # is serialised into this field; the operator submits unchanged.
    tiers_json: Annotated[str, Form()] = "[]",
) -> HTMLResponse | RedirectResponse:
    """Receive the reviewed/edited draft and upsert a FunderRow.

    Step F (Finding 1) extension: accepts the step C structured fields
    (contact, tiers, auto-decline, conditional requirements,
    notes_residual) so the first-time import path matches the
    re-extract path. Tier editing is intentionally not supported in
    this form — the operator either accepts the extracted tiers or
    re-extracts against a different PDF. A future "edit funder" form
    can offer per-tier editing.
    """
    try:
        tiers = _parse_tiers_json(tiers_json)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": f"tier payload invalid: {exc}"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        funder = FunderRow(
            name=name,
            accepts_stacking=accepts_stacking.lower() in {"true", "on", "yes", "1"},
            min_monthly_revenue=_decimal_or_none(min_monthly_revenue),
            min_avg_daily_balance=_decimal_or_none(min_avg_daily_balance),
            min_credit_score=_int_or_none(min_credit_score),
            min_months_in_business=_int_or_none(min_months_in_business),
            max_positions=_int_or_none(max_positions),
            min_advance=_decimal_or_none(min_advance),
            max_advance=_decimal_or_none(max_advance),
            max_nsf_tolerance=_int_or_none(max_nsf_tolerance),
            typical_factor_low=_decimal_or_none(typical_factor_low),
            typical_factor_high=_decimal_or_none(typical_factor_high),
            typical_holdback_low=_decimal_or_none(typical_holdback_low),
            typical_holdback_high=_decimal_or_none(typical_holdback_high),
            excluded_industries=tuple(
                s.strip() for s in excluded_industries.split(",") if s.strip()
            ),
            excluded_states=tuple(
                s.strip().upper() for s in excluded_states.split(",") if s.strip()
            ),
            # Finding 3 fix: notes is `str = ""`, not Optional. Was
            # `notes or None` (Pydantic ValidationError waiting to happen).
            notes=notes or "",
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            submission_email=submission_email,
            tiers=tiers,
            auto_decline_conditions=_parse_bullet_lines(auto_decline_conditions),
            conditional_requirements=_parse_bullet_lines(conditional_requirements),
            notes_residual=notes_residual or "",
        )
    except (ValueError, TypeError) as exc:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": f"validation error: {exc}"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        saved = repo.upsert(funder)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "funder_import.html.j2",
            {"error": f"upsert failed: {exc}"},
            status_code=status.HTTP_409_CONFLICT,
        )
    return RedirectResponse(
        f"/ui/funders/{saved.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/funders/new", response_class=HTMLResponse)
async def funder_new_form(request: Request) -> HTMLResponse:
    """Render the empty manual-create form for a new FunderRow.

    Mirrors ``merchant_new_form``. The PDF-import flow at
    ``/ui/funders/import`` remains the preferred path when a funder
    publishes a structured criteria sheet; this manual form covers
    funders whose terms only exist as conversation notes or ISO-
    agreement clauses.
    """
    return templates.TemplateResponse(
        request, "funder_form.html.j2", {"error": None}
    )


@router.post("/funders/new", response_class=HTMLResponse, response_model=None)
async def funder_new_submit(
    request: Request,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    name: Annotated[str, Form()],
    active: Annotated[str, Form()] = "true",
    # Hard gates
    min_monthly_revenue:    Annotated[str, Form()] = "",
    min_avg_daily_balance:  Annotated[str, Form()] = "",
    min_credit_score:       Annotated[str, Form()] = "",
    min_months_in_business: Annotated[str, Form()] = "",
    max_positions:          Annotated[str, Form()] = "",
    accepts_stacking:       Annotated[str, Form()] = "false",
    min_advance:            Annotated[str, Form()] = "",
    max_advance:            Annotated[str, Form()] = "",
    max_nsf_tolerance:      Annotated[str, Form()] = "",
    requires_coj:           Annotated[str, Form()] = "false",
    # Pricing envelope
    typical_factor_low:    Annotated[str, Form()] = "",
    typical_factor_high:   Annotated[str, Form()] = "",
    typical_holdback_low:  Annotated[str, Form()] = "",
    typical_holdback_high: Annotated[str, Form()] = "",
    # Exclusions (comma-separated)
    excluded_industries: Annotated[str, Form()] = "",
    excluded_states:     Annotated[str, Form()] = "",
    # Contact
    contact_name:     Annotated[str, Form()] = "",
    contact_phone:    Annotated[str, Form()] = "",
    contact_email:    Annotated[str, Form()] = "",
    submission_email: Annotated[str, Form()] = "",
    # Compliance
    charges_merchant_advance_fees:      Annotated[str, Form()] = "false",
    aegis_compensation_disclosure_text: Annotated[str, Form()] = "",
    # Operator content
    operator_notes: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Receive the manual create form and upsert a fresh FunderRow.

    Reuses the same ``FunderRepository.upsert`` write-path as the PDF
    import flow and ``scripts/audit/seed_shor_capital.py``. Tiers,
    auto_decline_conditions, conditional_requirements, notes and
    notes_residual are intentionally left at their defaults — those
    are extraction-time fields and the operator edits them later from
    the detail page.
    """
    try:
        funder = FunderRow(
            name=name,
            active=active.lower() in _TRUE_TOKENS,
            min_monthly_revenue=_decimal_or_none(min_monthly_revenue),
            min_avg_daily_balance=_decimal_or_none(min_avg_daily_balance),
            min_credit_score=_int_or_none(min_credit_score),
            min_months_in_business=_int_or_none(min_months_in_business),
            max_positions=_int_or_none(max_positions),
            accepts_stacking=accepts_stacking.lower() in _TRUE_TOKENS,
            min_advance=_decimal_or_none(min_advance),
            max_advance=_decimal_or_none(max_advance),
            max_nsf_tolerance=_int_or_none(max_nsf_tolerance),
            requires_coj=requires_coj.lower() in _TRUE_TOKENS,
            typical_factor_low=_decimal_or_none(typical_factor_low),
            typical_factor_high=_decimal_or_none(typical_factor_high),
            typical_holdback_low=_decimal_or_none(typical_holdback_low),
            typical_holdback_high=_decimal_or_none(typical_holdback_high),
            excluded_industries=_parse_csv_list(excluded_industries),
            excluded_states=_parse_csv_list(excluded_states, upper=True),
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            submission_email=submission_email,
            charges_merchant_advance_fees=(
                charges_merchant_advance_fees.lower() in _TRUE_TOKENS
            ),
            aegis_compensation_disclosure_text=aegis_compensation_disclosure_text,
            operator_notes=operator_notes,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        return _funder_form_error(
            request, str(exc), _funder_form_dict_from_locals(locals())
        )
    try:
        saved = repo.upsert(funder)
    except ValueError as exc:
        return _funder_form_error(
            request, str(exc), _funder_form_dict_from_locals(locals())
        )
    return RedirectResponse(
        f"/ui/funders/{saved.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/funders/{funder_id}", response_class=HTMLResponse)
async def funder_detail(
    request: Request,
    funder_id: UUID,
    repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    reextracted: int | None = None,
    reextract_error: str | None = None,
) -> HTMLResponse:
    """Render the funder detail page.

    ``reextracted`` and ``reextract_error`` are flash-style query params
    set by the re-extract route's 303 redirect. The template renders a
    green success banner or a yellow error banner accordingly.
    """
    try:
        funder = repo.get(funder_id)
    except FunderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "funder_detail.html.j2",
        {
            "funder": funder,
            "reextract_flash": bool(reextracted),
            "reextract_error": reextract_error,
        },
    )


@router.get(
    "/funders/{funder_id}/submit-modal", response_class=HTMLResponse
)
async def funder_submit_modal(
    request: Request,
    funder_id: UUID,
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    deal_repo: Annotated[DealRepository, Depends(get_deal_repository)],
) -> HTMLResponse:
    """HTMX fragment — merchant picker for 'Submit a deal to this funder'.

    Lists up to 50 most-recent analyzed deals (parse_status in
    {proceed, review}), sorted by fraud_score ascending (best AEGIS
    deals first). Each row links to the merchant's match panel with
    this funder pre-selected via ``?preselect_funder=<funder_id>``.
    """
    try:
        funder = funder_repo.get(funder_id)
    except FunderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    # Two queries — DealRepository.list_deals takes a single parse_status.
    # Operators submit from both clean ("proceed") and lower-confidence
    # ("review") parses, so we union the two and re-sort in Python.
    deals = [
        *deal_repo.list_deals(parse_status="proceed", limit=50),
        *deal_repo.list_deals(parse_status="review", limit=50),
    ]
    # Primary: fraud_score ascending (lower = better AEGIS deal).
    # Tiebreaker: created_at descending (newer wins).
    # Sentinel 999 keeps unparsed rows last.
    deals.sort(
        key=lambda d: (
            d.fraud_score if d.fraud_score is not None else 999,
            -d.created_at.timestamp(),
        )
    )
    deals = deals[:50]

    return templates.TemplateResponse(
        request,
        "funder_submit_modal.html.j2",
        {"funder": funder, "deals": deals},
    )


@router.get(
    "/funders/{funder_id}/reextract-modal", response_class=HTMLResponse
)
async def funder_reextract_modal(
    request: Request,
    funder_id: UUID,
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
) -> HTMLResponse:
    """HTMX fragment — upload form for re-extracting an existing funder's
    criteria PDF. Posts to /ui/funders/{funder_id}/reextract.
    """
    try:
        funder = funder_repo.get(funder_id)
    except FunderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return templates.TemplateResponse(
        request, "funder_reextract_modal.html.j2", {"funder": funder}
    )


@router.post(
    "/funders/{funder_id}/reextract",
    response_class=HTMLResponse,
    response_model=None,
)
async def funder_reextract(
    funder_id: UUID,
    pdf: Annotated[UploadFile, File()],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    llm: Annotated[LLMClient, Depends(get_llm)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> Response:
    """Re-run extraction against an updated criteria PDF for an existing funder.

    Replaces the extraction-shaped fields on the existing FunderRow with
    the new extraction's values, preserving admin metadata (id, name,
    active, created_at). Contact fields are preserved on a per-field
    basis when the new extraction yields an empty value (avoid blanking a
    known-good rep contact on a PDF that has no contact block).

    Atomically migrates legacy `notes` prose to `notes_residual` if the
    latter is empty AND notes is non-empty, then clears `notes` (which
    is reserved for operator-authored content going forward).

    Failure modes redirect back to the funder detail page with
    ``?reextract_error=<urlencoded message>`` so the operator sees what
    went wrong without losing context. Success redirects with
    ``?reextracted=1`` so the page can render a confirmation banner.
    """
    try:
        existing = funder_repo.get(funder_id)
    except FunderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    body = await pdf.read(_MAX_FUNDER_IMPORT_BYTES + 1)
    if not body:
        return _reextract_redirect(funder_id, error="PDF was empty")
    if len(body) > _MAX_FUNDER_IMPORT_BYTES:
        return _reextract_redirect(
            funder_id,
            error=f"PDF exceeds {_MAX_FUNDER_IMPORT_BYTES} bytes",
        )

    try:
        extraction = extract_funder_guidelines(body, llm)
    except FunderExtractionError as exc:
        return _reextract_redirect(funder_id, error=str(exc))

    draft = extraction.draft

    # Contact-preservation rule: per-field, keep the existing value when
    # the new extraction returns empty. Tiers / auto-decline /
    # conditional get the opposite treatment (wholesale replace) — empty
    # there means "the new PDF has no tier structure" and should land.
    def _keep_existing_if_empty(new: str, old: str) -> str:
        return new if new else old

    # Atomic notes migration: if the existing funder has legacy `notes`
    # prose AND notes_residual is empty, move notes into the residual
    # bucket before applying the new extraction's notes_residual. If both
    # have content, the new extraction's residual wins and the legacy
    # notes are preserved into residual as a divided block.
    new_notes = ""  # always empty after re-extract — reserved for operator UI
    if existing.notes and not existing.notes_residual:
        # Pure migration of legacy notes to residual; new extraction's
        # residual (if any) appends below.
        if draft.notes_residual:
            new_notes_residual = (
                existing.notes
                + "\n\n— [legacy notes; migrated by re-extract] —\n\n"
                + draft.notes_residual
            )
        else:
            new_notes_residual = existing.notes
    else:
        new_notes_residual = draft.notes_residual

    merged = FunderRow(
        # Preserved admin metadata
        id=existing.id,
        name=existing.name,
        active=existing.active,
        # Replaced from extraction
        min_monthly_revenue=draft.min_monthly_revenue,
        min_avg_daily_balance=draft.min_avg_daily_balance,
        min_credit_score=draft.min_credit_score,
        min_months_in_business=draft.min_months_in_business,
        max_positions=draft.max_positions,
        accepts_stacking=draft.accepts_stacking,
        min_advance=draft.min_advance,
        max_advance=draft.max_advance,
        max_nsf_tolerance=draft.max_nsf_tolerance,
        requires_coj=draft.requires_coj,
        aegis_compensation_disclosure_text=draft.aegis_compensation_disclosure_text,
        charges_merchant_advance_fees=draft.charges_merchant_advance_fees,
        typical_factor_low=draft.typical_factor_low,
        typical_factor_high=draft.typical_factor_high,
        typical_holdback_low=draft.typical_holdback_low,
        typical_holdback_high=draft.typical_holdback_high,
        excluded_industries=draft.excluded_industries,
        excluded_states=draft.excluded_states,
        tiers=draft.tiers,
        auto_decline_conditions=draft.auto_decline_conditions,
        conditional_requirements=draft.conditional_requirements,
        # Provenance
        guidelines_extracted_at=draft.guidelines_extracted_at,
        guidelines_source_pdf_hash=draft.guidelines_source_pdf_hash,
        # Contact: per-field preservation
        contact_name=_keep_existing_if_empty(draft.contact_name, existing.contact_name),
        contact_phone=_keep_existing_if_empty(draft.contact_phone, existing.contact_phone),
        contact_email=_keep_existing_if_empty(draft.contact_email, existing.contact_email),
        submission_email=_keep_existing_if_empty(
            draft.submission_email, existing.submission_email
        ),
        # Notes: atomic migration
        notes=new_notes,
        notes_residual=new_notes_residual,
        # Issue 5 (2026-05-27): operator_notes is operator-authored
        # commentary that must survive re-extractions. Always preserve
        # the existing value — the extraction prompt does not produce
        # this field and even if it did we would ignore it.
        operator_notes=existing.operator_notes,
    )

    funder_repo.upsert(merged)

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="funder.reextracted",
        subject_type="funder",
        subject_id=existing.id,
        details={
            "funder_name": existing.name,
            "old_pdf_sha256": existing.guidelines_source_pdf_hash,
            "new_pdf_sha256": _sha256_hex(body),
            "notes_migrated_to_residual": bool(
                existing.notes and not existing.notes_residual
            ),
            "tier_count_before": len(existing.tiers),
            "tier_count_after": len(merged.tiers),
            "overall_confidence": extraction.overall_confidence,
        },
    )

    return _reextract_redirect(funder_id, success=True)


def _reextract_redirect(
    funder_id: UUID,
    *,
    success: bool = False,
    error: str | None = None,
) -> RedirectResponse:
    """Build the 303 redirect back to the funder detail page with the
    appropriate query-string flag so the template can render a flash."""
    if error is not None:
        return RedirectResponse(
            f"/ui/funders/{funder_id}?reextract_error="
            + urllib.parse.quote(error[:500]),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        f"/ui/funders/{funder_id}?reextracted=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# Soft cap for operator notes so a stray paste doesn't dump a megabyte
# into the funder row. 10K chars = ~5 long paragraphs; tighten or
# loosen later if real usage shows we need it.
_OPERATOR_NOTES_MAX_CHARS = 10_000


@router.post(
    "/funders/{funder_id}/operator-notes",
    response_class=HTMLResponse,
    response_model=None,
)
async def funder_operator_notes_save(
    request: Request,
    funder_id: UUID,
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    # Default to "" so submitting an empty textarea counts as a clear,
    # not a 422 (Form() with no default rejects empty/missing values).
    operator_notes: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save operator-authored notes on a funder. HTMX swap target.

    Returns the operator-notes block partial so the page doesn't full-
    reload — the form swaps itself with a refreshed copy that shows
    the new value plus a "Saved" indicator.
    """
    try:
        existing = funder_repo.get(funder_id)
    except FunderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    # Trim + soft-cap. Truncation is silent — if operators routinely
    # bump the cap we'll surface a warning, not yet.
    new_value = operator_notes.strip()[:_OPERATOR_NOTES_MAX_CHARS]
    old_value = existing.operator_notes

    if new_value == old_value:
        # No-op save (operator clicked Save without editing). Don't
        # write an audit row for no change.
        funder = existing
        just_saved = True
    else:
        funder = existing.model_copy(update={"operator_notes": new_value})
        funder_repo.upsert(funder)
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="funder.operator_notes_updated",
            subject_type="funder",
            subject_id=existing.id,
            details={
                "funder_name": existing.name,
                "before_length": len(old_value),
                "after_length": len(new_value),
                "cleared": new_value == "" and old_value != "",
            },
        )
        just_saved = True

    return templates.TemplateResponse(
        request,
        "_operator_notes_block.html.j2",
        {"funder": funder, "just_saved": just_saved},
    )


@router.get("/merchants/{merchant_id}/match", response_class=HTMLResponse)
async def merchant_match(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    preselect_funder: UUID | None = None,
) -> HTMLResponse:
    """Phase 7B matched-funders panel.

    Builds a ScoreInput from the merchant + latest analysis, scores it,
    iterates over active funders, and renders Centrex-style cards
    (eligible / soft-concerns / hard-fails). Operator picks via the API.

    ``preselect_funder`` (optional query param) is set when the operator
    arrived from a funder detail page's "+ Submit a deal" picker — that
    funder's checkbox pre-checks UNLESS the funder is hard-failing for
    this merchant. In the hard-fail case a banner renders at top of the
    page explaining why, and the checkbox stays unchecked. Unknown
    funder UUID is rendered as the same "not eligible" banner with a
    generic reason; ``preselect_funder=None`` (default) renders the
    page exactly as before.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # Migration 034 guard — same shape as the no-document branch below.
    # Non-finalized merchants carry a placeholder business_name; scoring
    # them would invoke OFAC against the placeholder. Render the panel
    # with ``missing='not_finalized'`` so existing template wiring
    # surfaces "merchant not ready" without a special UI chunk.
    if not merchant.is_finalized:
        return templates.TemplateResponse(
            request,
            "merchant_match.html.j2",
            {
                "merchant": merchant,
                "missing": "not_finalized",
                "score_result": None,
                "matches": [],
                "score_window": None,
                "preselect_funder_id": None,
                "preselect_banner": None,
            },
        )

    items = _collect_analyzed_for_merchant(docs, merchant_id)
    if not items:
        return templates.TemplateResponse(
            request,
            "merchant_match.html.j2",
            {
                "merchant": merchant,
                "missing": "no_document",
                "score_result": None,
                "matches": [],
                "score_window": None,
                "preselect_funder_id": None,
                "preselect_banner": None,
            },
        )

    score_input = _score_input_multi_month(merchant, items)
    try:
        score_result = score_deal(score_input, ofac=ofac)
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    cards: list[dict[str, Any]] = []
    for funder in funder_repo.list_active():
        m = match_funder(funder, score_input, score_result)
        if m is None:
            continue
        cards.append(_match_card(funder, m, score_input))
    cards.sort(key=lambda c: c["match_score"], reverse=True)

    # Preselect: pre-check the matching funder's checkbox unless the
    # funder is hard-failing this merchant (color=red), in which case we
    # surface a banner with the reasons and leave the checkbox unchecked.
    preselect_id_str: str | None = None
    preselect_banner: dict[str, Any] | None = None
    if preselect_funder is not None:
        target_id = str(preselect_funder)
        matched_card = next(
            (c for c in cards if c["funder_id"] == target_id), None
        )
        if matched_card is None:
            try:
                f = funder_repo.get(preselect_funder)
                preselect_banner = {
                    "name": f.name,
                    "reasons": [
                        "This funder is not active or has no matchable "
                        "criteria for this merchant."
                    ],
                }
            except FunderNotFoundError:
                # Unknown UUID — silently ignore, render page normally.
                preselect_banner = None
        elif matched_card["color"] == "red" and matched_card["hard_reasons"]:
            # Real hard fail (revenue floor, excluded state, etc.) — banner
            # the reasons so the operator sees WHY this funder can't fund it.
            preselect_banner = {
                "name": matched_card["funder_name"],
                "reasons": matched_card["hard_reasons"],
            }
        else:
            # Either the card is green/yellow (qualifies), OR it's red
            # solely because the merchant's overall score tier is F
            # (qualifies for criteria but underwriting tier is too low).
            # In the tier-F edge case the template's disabled-checkbox
            # treatment already prevents accidental submit; no banner.
            preselect_id_str = target_id

    score_window = {
        "months_used": len(items),
        "period_start": score_input.statement_period_start,
        "period_end": score_input.statement_period_end,
        "any_manual_review": any(
            d.parse_status == "manual_review" for d, _ in items
        ),
    }

    funder_responses = _latest_funder_responses(audit, merchant_id)

    return templates.TemplateResponse(
        request,
        "merchant_match.html.j2",
        {
            "merchant": merchant,
            "missing": None,
            "score_result": score_result,
            "matches": cards,
            "score_window": score_window,
            "funder_responses": funder_responses,
            "preselect_funder_id": preselect_id_str,
            "preselect_banner": preselect_banner,
        },
    )


@router.post("/merchants/{merchant_id}/submit", response_model=None)
async def merchant_submit_to_funders(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    funder_ids: Annotated[list[str], Form()],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> Response:
    """Build per-funder submission CSVs and stream them as a ZIP.

    Operator-triggered from the match panel. ``funder_ids`` is the
    multi-select of funder UUIDs the operator chose to forward to.
    A single funder returns the CSV inline; multiple funders return a
    ZIP. Always audits ``deal.submit_to_funders`` regardless of count.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    requested_ids = _parse_funder_ids(funder_ids)
    if not requested_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no funders selected",
        )

    # Migration 034 guard — submission to funders requires a real,
    # named merchant. Refuse on provisional / needs_manual_naming.
    if not merchant.is_finalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "merchant is not finalized — name the merchant via intake "
                "before submitting to funders"
            ),
        )

    items = _collect_analyzed_for_merchant(docs, merchant_id)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no analyzed document — upload + parse first",
        )

    score_input = _score_input_multi_month(merchant, items)
    try:
        score_result = score_deal(score_input, ofac=ofac)
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    requested_set = set(requested_ids)
    matched: list[FunderMatch] = []
    for f in funder_repo.list_active():
        if f.id not in requested_set:
            continue
        m = match_funder(f, score_input, score_result)
        if m is None:
            continue
        matched.append(m)

    if not matched:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="none of the selected funders have configured criteria for this merchant",
        )

    files = build_submission_files(score_input, score_result, matched)

    merchant_slug = slugify(merchant.business_name)
    if len(files) == 1:
        only = files[0]
        download_bytes = only.csv_bytes
        download_filename = only.filename
        download_media = "text/csv; charset=utf-8"
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for sub in files:
                zf.writestr(sub.filename, sub.csv_bytes)
        download_bytes = buf.getvalue()
        download_filename = f"submission_{merchant_slug}.zip"
        download_media = "application/zip"

    # Render the PDF dossier (operator review + audit trail; historically
    # this was attached to a Zoho Deal, which the Close cutover removed).
    # WeasyPrint native libs ship on the Hetzner box; on Windows dev
    # they're absent and we log the OSError + continue without a PDF.
    # The submission flow MUST complete even if PDF rendering fails.
    dossier_pdf, dossier_filename = _maybe_render_dossier_pdf(
        merchant=merchant, docs=docs, ofac=ofac
    )

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="deal.submit_to_funders",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "funder_ids": [sub.funder_id for sub in files],
            "funder_names": [sub.funder_name for sub in files],
            "score_tier": score_result.tier,
            "score": score_result.score,
            "attachment_sha256": _sha256_hex(download_bytes),
            "attachment_filename": download_filename,
            "dossier_pdf_sha256": (
                _sha256_hex(dossier_pdf) if dossier_pdf is not None else None
            ),
            "dossier_pdf_filename": dossier_filename,
        },
    )

    # Update tracking fields (in-memory implementations only — Supabase
    # path round-trips lose these; durable record is the audit row above).
    try:
        merchants.upsert(
            merchant.model_copy(
                update={
                    "submitted_to_funder_ids": [
                        UUID(sub.funder_id) for sub in files
                    ],
                    "last_submitted_at": datetime.now(UTC),
                }
            )
        )
    except Exception as exc:
        # Tracking is best-effort; the audit row above is authoritative.
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "submission.tracking_update_failed merchant_id=%s err=%s",
            merchant.id,
            exc,
        )

    # Submission CRM-side sync removed during the Close cutover. The
    # audit row written above is the durable record of the submission.
    # If/when the Close-side submission custom-activity gets built (see
    # docs/research/close-integration-design.md "Out of scope"), the
    # call site lands here.

    return Response(
        content=download_bytes,
        media_type=download_media,
        headers={
            "content-disposition": f'attachment; filename="{download_filename}"'
        },
    )


_FUNDER_RESPONSE_STATUSES = frozenset({"approved", "declined", "countered", "pending"})


@router.post("/merchants/{merchant_id}/funder-response", response_model=None)
async def merchant_funder_response(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    funder_id: Annotated[str, Form()],
    response_status: Annotated[str, Form()],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    offered_amount: Annotated[str, Form()] = "",
    offered_factor: Annotated[str, Form()] = "",
    offered_term_days: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> Response:
    """Record one funder's reply to an AEGIS submission.

    v1 persistence is via ``audit_log`` only — the durable submissions
    table is Phase 7C. Reads pull the latest row per funder back through
    ``audit.list_for_subject(action='deal.funder_response')``, so the
    merchant-match panel always shows what the operator last typed.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    rs = response_status.strip().lower()
    if rs not in _FUNDER_RESPONSE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"response_status must be one of {sorted(_FUNDER_RESPONSE_STATUSES)}",
        )

    try:
        funder_uuid = UUID(funder_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid funder_id: {funder_id!r}",
        ) from exc

    try:
        funder = funder_repo.get(funder_uuid)
    except FunderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    try:
        amount = _decimal_or_none(offered_amount)
        factor = _decimal_or_none(offered_factor)
        term = _int_or_none(offered_term_days)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="deal.funder_response",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "funder_id": str(funder_uuid),
            "funder_name": funder.name,
            "status": rs,
            "offered_amount": str(amount) if amount is not None else None,
            "offered_factor": str(factor) if factor is not None else None,
            "offered_term_days": term,
            "notes": notes.strip() or None,
        },
    )

    # Redirect back to the match panel so the operator sees the row land.
    return RedirectResponse(
        url=f"/ui/merchants/{merchant.id}/match", status_code=303
    )


# ---------------------------------------------------------------------------
# Close attachment rescan (Feature 2, chunk 5).
# ---------------------------------------------------------------------------


@router.post("/merchants/{merchant_id}/close-rescan", response_model=None)
async def merchant_close_rescan(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    override_cap: Annotated[
        bool,
        Query(
            description=(
                "Set true to bypass the close_attachment_hard_cap when a "
                "previous run hit the cap. The chunk-5 UI surfaces a "
                "second 'Rescan all (override cap)' button when the most "
                "recent orchestration audit row had capped=true."
            ),
        ),
    ] = False,
) -> RedirectResponse:
    """Operator-clicked manual rescan of a merchant's Close attachments.

    Enqueues ``process_close_attachments(close_lead_id, 'rescan',
    actor_email=..., override_cap=...)``. Audit trail mirrors the
    webhook path so the merchant detail history panel surfaces both
    auto and manual runs.

    404 if the merchant has no ``close_lead_id``. The button on the
    merchant detail template is only rendered when ``close_lead_id``
    is set, but a stale browser tab could still POST against a
    just-unlinked merchant — 404 is honest.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    if merchant.close_lead_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"merchant {merchant_id} has no close_lead_id; rescan "
                "requires a linked Close Lead"
            ),
        )

    await enqueue_close_orchestration(
        request=request,
        close_lead_id=merchant.close_lead_id,
        merchant_id=merchant.id,
        audit=audit,
        trigger="rescan",
        actor_email=actor_email,
        override_cap=override_cap,
    )

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="close.orchestration.manual_rescan",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "close_lead_id": merchant.close_lead_id,
            "override_cap": override_cap,
        },
    )

    return RedirectResponse(
        url=f"/ui/merchants/{merchant.id}", status_code=303
    )


def _close_orchestration_last_capped(history: list[dict[str, Any]]) -> bool:
    """True iff the most recent ``close.orchestration.complete`` row
    for the merchant landed with ``capped=True``.

    Drives the cap-override button visibility on the merchant detail
    template (chunk 5). ``history`` is already newest-first per the
    ``list_for_subject`` contract; we walk it once and stop at the
    first orchestration-complete row. Older rows are not consulted —
    operator behavior is "did the latest run cap?", not "did any
    historical run cap?".
    """
    for row in history:
        if row.get("action") == "close.orchestration.complete":
            details = row.get("details") or {}
            return bool(details.get("capped"))
    return False


def _latest_funder_responses(
    audit: AuditLog, merchant_id: UUID
) -> dict[str, dict[str, Any]]:
    """Pull latest ``deal.funder_response`` audit row per funder_id.

    Keyed by funder_id (string UUID). Empty dict if none recorded yet.
    Used by the match panel to render a status chip per submitted lender.
    """
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant_id,
        action="deal.funder_response",
        limit=200,
    )
    out: dict[str, dict[str, Any]] = {}
    # rows are newest-first, so the first row we see per funder_id wins.
    for r in rows:
        details = r.get("details") or {}
        fid = details.get("funder_id")
        if not isinstance(fid, str) or fid in out:
            continue
        out[fid] = {
            "status": details.get("status"),
            "offered_amount": details.get("offered_amount"),
            "offered_factor": details.get("offered_factor"),
            "offered_term_days": details.get("offered_term_days"),
            "notes": details.get("notes"),
            "recorded_at": r.get("created_at"),
        }
    return out


def _sha256_hex(payload: bytes) -> str:
    """Cheap content-addressable handle for an audit-log attachment row."""
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _maybe_render_dossier_pdf(
    *,
    merchant: MerchantRow,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> tuple[bytes | None, str | None]:
    """Render the merchant's PDF dossier (operator review + audit), or fail soft.

    Returns ``(pdf_bytes, filename)`` on success; ``(None, None)`` if the
    Hetzner box / WSL2 native libs are unavailable. The submission flow
    must not fail just because a PDF can't be produced — the CSV ZIP
    download and the audit row are the authoritative record.
    """
    try:
        import weasyprint

        context = _build_pdf_dossier_context(merchant, docs, ofac)
        html = templates.get_template("merchant_detail_dossier_pdf.html.j2").render(
            context
        )
        pdf_bytes = cast(bytes, weasyprint.HTML(string=html).write_pdf())
        filename = f"{slugify(merchant.business_name)}_dossier.pdf"
        return pdf_bytes, filename
    except (OSError, ImportError) as exc:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "dossier_pdf_render_failed merchant_id=%s err=%s",
            merchant.id,
            exc,
        )
        return None, None


def _parse_funder_ids(values: list[str]) -> list[UUID]:
    """Coerce form-encoded funder_id values into a deduped list of UUIDs."""
    out: list[UUID] = []
    seen: set[UUID] = set()
    for v in values:
        s = v.strip()
        if not s:
            continue
        try:
            u = UUID(s)
        except ValueError:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _dossier_pattern_analysis(
    latest_analysis: AnalysisRow,
    latest_transactions: list[ClassifiedTransaction],
) -> PatternAnalysis | None:
    """Return a ``PatternAnalysis`` for the dossier render — prefer the
    stored cache, recompute as fallback for legacy rows.

    Closes backlog item #6 — the dossier no longer pays the
    ``analyze_patterns()`` cost on every render. ``AnalysisRow.pattern_analysis``
    is populated on every new analysis since stage 2 chunk 2 deployed
    (migration 032, 2026-05-29), so the common path is one DTO →
    dataclass conversion. Rows analyzed before chunk 2 still carry
    ``pattern_analysis=None`` and fall back to live recomputation, so
    the dossier render keeps working across the legacy / fresh split
    until those rows age out.

    Recomputation is wrapped in ``try/except → None`` mirroring the
    historical call-site behavior: defensive against malformed
    transactions so the dossier render never crashes on a single
    statement. The ``from_dto`` path doesn't need a guard — the DTO
    was validated by Pydantic at storage time.
    """
    if latest_analysis.pattern_analysis is not None:
        return pattern_analysis_from_dto(latest_analysis.pattern_analysis)
    try:
        return analyze_patterns(
            latest_transactions,
            latest_analysis.statement_period_start,
            latest_analysis.statement_period_end,
        )
    except Exception:
        return None


@router.get("/merchants/{merchant_id}", response_class=HTMLResponse)
async def merchant_detail(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # Flash from /ui/intake — broker just created the merchant. Banner
    # rendered by the template when from_intake=1 is present in the URL.
    from_intake = request.query_params.get("from_intake") == "1"
    try:
        intake_docs_uploaded = int(request.query_params.get("docs") or "0")
    except ValueError:
        intake_docs_uploaded = 0
    try:
        intake_docs_failed = int(request.query_params.get("failed") or "0")
    except ValueError:
        intake_docs_failed = 0

    all_docs = docs.list_documents(merchant_id=merchant_id, limit=50)
    # Batch fetch analyses for every document in one query rather than
    # N+1 per-document calls. analyses_by_doc.get(doc.id) yields the
    # AnalysisRow when present, None when the document hasn't been
    # parsed yet.
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in all_docs])
    documents_table: list[dict[str, Any]] = [
        {"document": d, "analysis": analyses_by_doc.get(d.id)} for d in all_docs
    ]

    latest_doc = all_docs[0] if all_docs else None
    latest_analysis = analyses_by_doc.get(latest_doc.id) if latest_doc else None

    # Bundle switcher: ?bundle=<bank>|<last4>. Empty segments mean
    # "unknown" (encoded back as None). Falls back to most-populated
    # bundle when the param is absent or names a bundle that no longer
    # exists for this merchant.
    selected_bundle = _parse_bundle_query(request.query_params.get("bundle"))

    score_result = None
    stacking = None
    score_window = None
    bundle_summaries: list[dict[str, Any]] = []
    statement_coverage: dict[str, Any] | None = None
    pattern_cards: list[Any] = []
    # Surfaced to the dossier template so the evidence drill-down's
    # preloan_spike baseline panel can look beyond ``card.source_transactions``
    # and pull pre-spike deposits for the comparison reference.
    latest_transactions: list[Any] = []
    soft_signals = (
        parse_soft_signal_flags(list(latest_doc.all_flags))
        if latest_doc is not None
        else None
    )
    # Held for the post-template ``has_concentration_pattern`` check so
    # the suppression doesn't re-walk the AnalysisRow cache.
    pattern_analysis_for_view: Any = None
    if latest_doc is not None and latest_analysis is not None:
        all_items = _collect_analyzed_for_merchant(
            docs, merchant_id, window=999, bundle=None
        )
        bundle_options = _bundle_keys_for_merchant(all_items)
        if selected_bundle is not None and selected_bundle not in {
            k for k, _ in bundle_options
        }:
            selected_bundle = None
        items = _collect_analyzed_for_merchant(
            docs, merchant_id, bundle=selected_bundle
        )
        active_bundle = (
            selected_bundle
            if selected_bundle is not None
            else _select_default_bundle(all_items)
        )
        bundle_summaries = _build_bundle_summaries(bundle_options, active_bundle)
        # Pattern analysis is sourced from the AnalysisRow cache
        # populated by stage 2 chunk 2 (migration 032), with a
        # transactions-based recomputation as fallback for legacy rows
        # — see ``_dossier_pattern_analysis``. Also feeds ``score_input``
        # so the counterparty / detector signals reach the scorer
        # (master plan §9).
        latest_transactions = docs.list_transactions(latest_doc.id)
        stacking = build_stacking_card(latest_analysis, latest_transactions)
        pattern_analysis = _dossier_pattern_analysis(
            latest_analysis, latest_transactions
        )
        pattern_analysis_for_view = pattern_analysis
        # Migration 034 — scoring requires a finalized merchant.
        # Skip the score panel + statement_coverage build for
        # provisional / needs_manual_naming; the dossier still
        # renders, just without the score-derived sections.
        if items and merchant.is_finalized:
            score_input = _score_input_multi_month(
                merchant, items, pattern_analysis=pattern_analysis
            )
            try:
                score_result = score_deal(score_input, ofac=ofac)
            except OFACStaleError:
                score_result = None
            score_window = {
                "months_used": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "any_manual_review": any(
                    d.parse_status == "manual_review" for d, _ in items
                ),
            }
            statement_coverage = {
                "bundle_bank_name": active_bundle[0] if active_bundle else None,
                "bundle_account_last4": active_bundle[1] if active_bundle else None,
                "statements_in_bundle": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "missing_months": _detect_missing_months(items),
                "bundle_options": bundle_summaries,
            }
        # Build pattern cards AFTER scoring runs so we can suppress the
        # display card for patterns that the scorer already attached as a
        # hard-decline reason (v2 catalog Bucket B.7 — dual-tier
        # acceleration / withdrawal-dispute presentation). Cards still
        # build on the non-finalized branch with ``hard_decline_reasons``
        # absent — the worker sees every pattern card until scoring
        # runs.
        _hard_decline_reasons = (
            list(score_result.hard_decline_reasons) if score_result else None
        )
        pattern_cards = list(
            build_pattern_cards(
                pattern_analysis,
                latest_transactions,
                hard_decline_reasons=_hard_decline_reasons,
            )
        )

    state_tier = _state_tier(merchant.state)
    # OFAC screening is REGULATORY — a screening row written for a
    # placeholder business name ("(awaiting parse)") would fabricate a
    # compliance artifact ("we screened (awaiting parse)") that future
    # audits could not distinguish from a real screening. Gate on
    # ``is_finalized`` so OFAC fires only on real names. Non-finalized
    # merchants surface ``ofac_status='not_consulted'`` in the ribbon.
    if merchant.is_finalized:
        ofac_status, ofac_match = _ofac_ribbon_status(
            ofac, merchant.business_name
        )
    else:
        ofac_status, ofac_match = ("not_consulted", None)

    from aegis.api.routes.findings import _compute_trend

    trend = _compute_trend(all_docs, docs)
    history = audit.list_for_subject(
        subject_type="merchant", subject_id=merchant_id, limit=20
    )
    close_last_orchestration_capped = _close_orchestration_last_capped(history)

    # Dossier is the only merchant-detail surface. The legacy v2 panel
    # template was retired when the whole app was unified on the dossier
    # aesthetic; ?view=v2 is accepted but ignored (kept reachable so any
    # bookmarked link still 200s instead of 404ing).
    template_name = "merchant_detail_dossier.html.j2"

    # Reshape state_tier into the richer dict the dossier template
    # expects. v2 template ignores extra keys. Citation / verified are
    # sourced from the STATES registry when present.
    state_reg = STATES.get(merchant.state.upper()) if merchant.state else None
    state_tier_dossier: dict[str, Any] | None = None
    if isinstance(state_tier, int):
        tier_summaries = {
            1: "Commercial-finance disclosure law applies. Pre-signature disclosure required.",
            2: "General state law applies. No MCA-specific statute; standard contract law governs.",
            3: "Served but not yet audited. Disclosure renderer raises StateNotAudited.",
        }
        state_tier_dossier = {
            "label": f"Tier {['', 'I', 'II', 'III'][state_tier]}",
            "summary": tier_summaries.get(state_tier, ""),
            "citation": getattr(state_reg, "citation_url", None)
            or getattr(state_reg, "statute_citation", None),
            "verified": getattr(state_reg, "verified_date", None),
        }

    # Map _ofac_ribbon_status output into the dossier's status keys.
    if ofac_status == "checked":
        ofac_dossier_status = "match" if ofac_match else "clean"
    elif ofac_status == "stale":
        ofac_dossier_status = "unavailable"
    elif ofac_status == "unavailable":
        ofac_dossier_status = "unavailable"
    else:
        ofac_dossier_status = "pending"

    # v2 catalog Bucket B.1 — soft_signals.customer_concentration and a
    # ``customer_concentration`` Pattern both describe the same fact (top
    # counterparty share of revenue). When both fire the dossier used to
    # render two cards side-by-side; suppress the soft-signals one when
    # the richer pattern card is already on the page.
    _has_concentration_pattern = pattern_has_customer_concentration(
        pattern_analysis_for_view
    )

    # Build the unified A+B+C view alongside the existing score block.
    # Pure presentation; no decline-path impact. Step 2 of the scoring
    # redesign retires fraud_score and flips A/B/C live; this commit
    # only adds the surface.
    from aegis.scoring_v2.dossier_panel import build_unified_tracks_view

    unified_tracks = build_unified_tracks_view(
        documents=all_docs,
        list_transactions=docs.list_transactions,
        analyses_by_doc=analyses_by_doc,
    )

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "merchant": merchant,
            "documents": documents_table,
            "document": latest_doc,
            "analysis": latest_analysis,
            "aggregate_labels": _AGGREGATE_LABELS,
            "aggregate_unit_kind": _AGGREGATE_UNIT_KIND,
            "pattern_cards": pattern_cards,
            "latest_transactions": latest_transactions,
            "soft_signals": soft_signals,
            "has_concentration_pattern": _has_concentration_pattern,
            "from_intake": from_intake,
            "intake_docs_uploaded": intake_docs_uploaded,
            "intake_docs_failed": intake_docs_failed,
            "score_result": score_result,
            "score_window": score_window,
            "statement_coverage": statement_coverage,
            "stacking": stacking,
            "state_tier": state_tier_dossier,
            "ofac_status": ofac_dossier_status,
            "ofac_match": ofac_match,
            "trend": trend,
            "history": history,
            "close_last_orchestration_capped": close_last_orchestration_capped,
            "unified_tracks": unified_tracks,
        },
    )


@router.get("/merchants/{merchant_id}/dossier.pdf")
async def merchant_dossier_pdf(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> Response:
    """Downloadable PDF dossier of the merchant.

    Same data as the HTML dossier at ``/ui/merchants/{id}`` but laid out
    for paper: US Letter, page-break controls, no HTMX, no sidebar,
    system fonts. Always renders the *default* bundle (most-populated
    bank/last4 pair); operators switching bundles on the dashboard get
    the on-screen view, not a separate PDF per bundle.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    context = _build_pdf_dossier_context(merchant, docs, ofac)
    template = templates.get_template("merchant_detail_dossier_pdf.html.j2")
    html = template.render(context)

    # WeasyPrint native libs (Pango / Cairo / HarfBuzz) ship on the
    # Hetzner production box via deploy/install.sh. Local Windows dev
    # boxes don't have them — both the import itself and the render
    # can OSError when libgobject / libpango aren't on the loader path.
    # Operators developing on Windows should use WSL2 (documented in
    # README) and see a useful 503 here instead of a 500 stack trace.
    try:
        import weasyprint

        pdf_bytes = cast(bytes, weasyprint.HTML(string=html).write_pdf())
    except (OSError, ImportError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"weasyprint native libs unavailable: {exc}. Run from "
                "WSL2 / Linux, or use the Hetzner production deploy."
            ),
        ) from exc

    filename = f"{slugify(merchant.business_name)}_dossier.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _build_pdf_dossier_context(
    merchant: MerchantRow,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> dict[str, Any]:
    """Build the print-template context (subset of merchant_detail's context).

    The print template only needs the fields it actually renders. This
    helper keeps the PDF route concise and the data sourcing identical
    to the HTML dossier (same scoring, same bundle pick, same pattern
    cards, same OFAC ribbon).
    """
    all_docs = docs.list_documents(merchant_id=merchant.id, limit=50)
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in all_docs])
    documents_table: list[dict[str, Any]] = [
        {"document": d, "analysis": analyses_by_doc.get(d.id)} for d in all_docs
    ]

    latest_doc = all_docs[0] if all_docs else None
    latest_analysis = analyses_by_doc.get(latest_doc.id) if latest_doc else None

    score_result = None
    score_window = None
    statement_coverage: dict[str, Any] | None = None
    stacking = None
    pattern_cards: list[Any] = []
    pattern_analysis_for_view: Any = None

    if latest_doc is not None and latest_analysis is not None:
        all_items = _collect_analyzed_for_merchant(
            docs, merchant.id, window=999, bundle=None
        )
        bundle_options = _bundle_keys_for_merchant(all_items)
        items = _collect_analyzed_for_merchant(docs, merchant.id, bundle=None)
        active_bundle = _select_default_bundle(all_items)

        # Pattern analysis sourced from the AnalysisRow cache with a
        # recomputation fallback for legacy rows
        # (``_dossier_pattern_analysis``). Built BEFORE score_input so
        # the counterparty + master-plan §9 detector signals reach the
        # scorer.
        latest_transactions = docs.list_transactions(latest_doc.id)
        stacking = build_stacking_card(latest_analysis, latest_transactions)
        pattern_analysis = _dossier_pattern_analysis(
            latest_analysis, latest_transactions
        )
        pattern_analysis_for_view = pattern_analysis

        # Migration 034 — same is_finalized scoring gate as the
        # matched-funders dossier branch above.
        if items and merchant.is_finalized:
            score_input = _score_input_multi_month(
                merchant, items, pattern_analysis=pattern_analysis
            )
            try:
                score_result = score_deal(score_input, ofac=ofac)
            except OFACStaleError:
                score_result = None
            score_window = {
                "months_used": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "any_manual_review": any(
                    d.parse_status == "manual_review" for d, _ in items
                ),
            }
            statement_coverage = {
                "bundle_bank_name": active_bundle[0] if active_bundle else None,
                "bundle_account_last4": active_bundle[1] if active_bundle else None,
                "statements_in_bundle": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "missing_months": _detect_missing_months(items),
                "bundle_options": _build_bundle_summaries(bundle_options, active_bundle),
            }

        # Pattern cards built AFTER scoring so dual-tier signals
        # (acceleration_clause_triggered / unauthorized_withdrawal_dispute)
        # don't render once as a hard-decline line + once as a soft
        # severity card. See ``build_pattern_cards.hard_decline_reasons``.
        _hard_decline_reasons = (
            list(score_result.hard_decline_reasons) if score_result else None
        )
        pattern_cards = list(
            build_pattern_cards(
                pattern_analysis,
                latest_transactions,
                hard_decline_reasons=_hard_decline_reasons,
            )
        )

    state_tier = _state_tier(merchant.state)
    state_reg = STATES.get(merchant.state.upper()) if merchant.state else None
    state_tier_dossier: dict[str, Any] | None = None
    if isinstance(state_tier, int):
        tier_summaries = {
            1: "Commercial-finance disclosure law applies. Pre-signature disclosure required.",
            2: "General state law applies. No MCA-specific statute; standard contract law governs.",
            3: "Served but not yet audited. Disclosure renderer raises StateNotAudited.",
        }
        state_tier_dossier = {
            "label": f"Tier {['', 'I', 'II', 'III'][state_tier]}",
            "summary": tier_summaries.get(state_tier, ""),
            "citation": getattr(state_reg, "citation_url", None)
            or getattr(state_reg, "statute_citation", None),
            "verified": getattr(state_reg, "verified_date", None),
        }

    # Same is_finalized OFAC gate as the matched-funders dossier above —
    # never screen a placeholder name. Non-finalized merchants render
    # with the "pending" ribbon (the existing "not_consulted" arm).
    if merchant.is_finalized:
        ofac_status_raw, ofac_match = _ofac_ribbon_status(
            ofac, merchant.business_name
        )
    else:
        ofac_status_raw, ofac_match = ("not_consulted", None)
    if ofac_status_raw == "checked":
        ofac_dossier_status = "match" if ofac_match else "clean"
    elif ofac_status_raw == "stale":
        ofac_dossier_status = "unavailable"
    elif ofac_status_raw == "unavailable":
        ofac_dossier_status = "unavailable"
    else:
        ofac_dossier_status = "pending"

    return {
        "merchant": merchant,
        "document": latest_doc,
        "analysis": latest_analysis,
        "documents": documents_table,
        "score_result": score_result,
        "score_window": score_window,
        "statement_coverage": statement_coverage,
        "stacking": stacking,
        "pattern_cards": pattern_cards,
        "has_concentration_pattern": pattern_has_customer_concentration(
            pattern_analysis_for_view
        ),
        "state_tier": state_tier_dossier,
        "ofac_status": ofac_dossier_status,
        "ofac_match": ofac_match,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }


@router.get("/merchants/{merchant_id}/findings.csv")
async def merchant_findings_csv(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
) -> Response:
    """CSV download of the findings payload (no bearer; same trust model as the panel)."""
    from aegis.api.routes.findings import build_merchant_findings
    from aegis.web._findings_csv import findings_to_csv

    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    findings = build_merchant_findings(merchant=merchant, docs=docs, ofac=ofac)
    body = findings_to_csv(findings)
    filename = f"findings_{slugify(merchant.business_name)}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/documents/{document_id}",
    response_class=HTMLResponse,
    summary="Statement detail — metadata + aggregates + every classified transaction.",
)
async def document_detail(
    request: Request,
    document_id: UUID,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    try:
        document = docs.get_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    analysis = docs.get_analysis(document_id)
    transactions = docs.list_transactions(document_id)

    merchant: MerchantRow | None = None
    if document.merchant_id is not None:
        try:
            merchant = merchants_repo.get(document.merchant_id)
        except MerchantNotFoundError:
            merchant = None

    # Category histogram for the in-page filter strip.
    category_counts: dict[str, int] = {}
    for t in transactions:
        category_counts[t.category] = category_counts.get(t.category, 0) + 1

    # Build a {tx_id -> set of aggregates that source-id back to it} map
    # so each row can show which aggregates it contributed to.
    contributes: dict[UUID, list[str]] = {}
    if analysis is not None:
        for agg, field in _AGGREGATE_SOURCE_FIELDS.items():
            for src_id in getattr(analysis, field, []):
                contributes.setdefault(src_id, []).append(_AGGREGATE_LABELS[agg])

    return templates.TemplateResponse(
        request,
        "document_detail.html.j2",
        {
            "document": document,
            "analysis": analysis,
            "transactions": transactions,
            "merchant": merchant,
            "category_counts": category_counts,
            "contributes": contributes,
            "aggregate_labels": _AGGREGATE_LABELS,
        },
    )


@router.get(
    "/documents/{document_id}/aggregate/{aggregate}",
    response_class=HTMLResponse,
    summary="HTMX partial — transactions that contributed to an aggregate.",
)
async def aggregate_drilldown(
    request: Request,
    document_id: UUID,
    aggregate: str,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
) -> HTMLResponse:
    if aggregate not in _AGGREGATE_SOURCE_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown aggregate: {aggregate!r}",
        )

    try:
        docs.get_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    analysis = docs.get_analysis(document_id)
    if analysis is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no analysis for document"
        )

    source_ids: list[UUID] = list(getattr(analysis, _AGGREGATE_SOURCE_FIELDS[aggregate]))
    all_txs = docs.list_transactions(document_id)
    contributing = [t for t in all_txs if t.id in set(source_ids)]

    return templates.TemplateResponse(
        request,
        "_transactions_partial.html.j2",
        {
            "transactions": contributing,
            "aggregate_label": _AGGREGATE_LABELS[aggregate],
        },
    )


# Helpers --------------------------------------------------------------------


def _find_latest_for_merchant(
    docs: DocumentRepository,
    merchant_id: UUID,
) -> tuple[DocumentRow | None, AnalysisRow | None]:
    """Return the most recently uploaded document + analysis for a merchant.

    Uses the Protocol's ``list_documents`` filter so both the in-memory and
    Supabase repositories are exercised through the same indexed path.
    """
    rows = docs.list_documents(merchant_id=merchant_id, limit=1)
    if not rows:
        return None, None
    latest = rows[0]
    return latest, docs.get_analysis(latest.id)


# Hardcoded window: include the trailing 3 statement months in the
# multi-month score. Funder underwriting industry-norm is "last 3 bank
# statements" — more isn't more informative because business conditions
# change. Configurable later if a specific funder asks for 4 or 6.
_SCORE_WINDOW_MONTHS: int = 3


BundleKey = tuple[str | None, str | None]


# Trailing suffixes that distinguish one printed bank-name variant from
# another (e.g. "Bank of America" vs "Bank of America, N.A.") without
# representing a different institution. Stripped during bundle keying so
# the same account at the same bank groups under one bundle regardless
# of which suffix the statement happened to print this month.
_BANK_NAME_SUFFIXES = (
    ", n. a.",
    ", n.a.",
    " n.a.",
    ", national association",
)


def _normalize_bank_name(name: str | None) -> str | None:
    """Lowercase, strip, drop trailing institution-type suffixes.

    Used only for bundle keying — UI label rendering reads the raw,
    unnormalized ``analysis.bank_name`` so operators still see "Bank of
    America, N.A." in the picker.
    """
    if name is None:
        return None
    s = name.strip().lower()
    if not s:
        return None
    for suffix in _BANK_NAME_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip(", ")
            break
    return s.strip() or None


def _bundle_key(analysis: AnalysisRow) -> BundleKey:
    """The (normalized bank_name, account_last4) key used to group statements.

    Verified VU Development 2026-06: three docs printed
    "Bank of America, N.A." and one printed "Bank of America" with the
    same account_last4. Pre-normalization the singleton was dropped from
    the default bundle.
    """
    return (_normalize_bank_name(analysis.bank_name), analysis.account_last4)


def _bundle_keys_for_merchant(
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> list[tuple[BundleKey, int]]:
    """Return distinct bundle keys with their statement counts, most-populated first.

    Ties broken by latest ``statement_period_end`` so the most-recent
    bundle wins when two accounts have equal statement counts.
    """
    counts: dict[BundleKey, int] = {}
    latest_end: dict[BundleKey, date] = {}
    for _, analysis in items:
        key = _bundle_key(analysis)
        counts[key] = counts.get(key, 0) + 1
        if (
            key not in latest_end
            or analysis.statement_period_end > latest_end[key]
        ):
            latest_end[key] = analysis.statement_period_end

    def _sort_key(item: tuple[BundleKey, int]) -> tuple[int, date]:
        key, count = item
        return (count, latest_end[key])

    return sorted(counts.items(), key=_sort_key, reverse=True)


def _select_default_bundle(
    items: list[tuple[DocumentRow, AnalysisRow]],
) -> BundleKey | None:
    """Pick the most-populated bundle, or ``None`` if no items.

    See ``_bundle_keys_for_merchant`` for the tiebreak rule.
    """
    keys = _bundle_keys_for_merchant(items)
    return keys[0][0] if keys else None


def _filter_to_bundle(
    items: list[tuple[DocumentRow, AnalysisRow]],
    bundle: BundleKey,
) -> list[tuple[DocumentRow, AnalysisRow]]:
    """Keep only items whose ``(bank_name, account_last4)`` matches ``bundle``.

    Normalizes the incoming ``bundle`` so callers can pass raw
    ``(bank_name, last4)`` tuples without first running them through
    ``_normalize_bank_name`` themselves.
    """
    bank, last4 = bundle
    normalized = (_normalize_bank_name(bank), last4)
    return [(d, a) for d, a in items if _bundle_key(a) == normalized]


def _bundle_to_query(bundle: BundleKey) -> str:
    """Encode a bundle key as the ``?bundle=`` query value (``bank|last4``)."""
    bank, last4 = bundle
    return f"{bank or ''}|{last4 or ''}"


def _parse_bundle_query(value: str | None) -> BundleKey | None:
    """Parse a ``?bundle=`` query value back into a bundle key.

    Returns ``None`` for missing / blank / malformed inputs (caller then
    picks the default bundle). Empty segments are mapped to ``None`` so
    a pre-migration ``(None, None)`` bundle is addressable as ``|``.
    """
    if not value:
        return None
    parts = value.split("|", 1)
    if len(parts) != 2:
        return None
    bank, last4 = parts
    # Normalize so an old bookmark with "Bank of America, N.A.|7719"
    # resolves to the same bundle as the new "bank of america|7719".
    return (_normalize_bank_name(bank or None), last4 or None)


def _build_bundle_summaries(
    bundle_options: list[tuple[BundleKey, int]],
    active: BundleKey | None,
) -> list[dict[str, Any]]:
    """Template-friendly view of the merchant's bundles.

    Each entry carries the bank/last4 labels, the statement count, the
    URL-safe query value, and whether the bundle is currently active.
    """
    out: list[dict[str, Any]] = []
    for key, count in bundle_options:
        bank, last4 = key
        out.append(
            {
                "bank_name": bank,
                "account_last4": last4,
                "count": count,
                "query": _bundle_to_query(key),
                "is_active": key == active,
            }
        )
    return out


def _collect_analyzed_for_merchant(
    docs: DocumentRepository,
    merchant_id: UUID,
    *,
    window: int = _SCORE_WINDOW_MONTHS,
    bundle: BundleKey | None = None,
) -> list[tuple[DocumentRow, AnalysisRow]]:
    """Return up to ``window`` most-recent analyzed docs for a merchant.

    "Analyzed" means the document has an analysis row — i.e. extraction +
    validation + classification + aggregation all completed. ``manual_review``
    status is OK if the analysis exists (classification-confidence floor
    breaches still produce a usable analysis; the operator's decision is
    informed by including them).

    Returned newest first so the caller can pick the latest doc as the
    "current state" anchor and use the remainder as historical context.

    Bundling
    --------
    A merchant with two bank accounts produces two bundles of statements.
    Scoring across mixed-account statements is wrong: revenue sums across
    accounts double-count cash that just moved between them. The default
    behavior here is therefore "pick the most-populated bundle" — pass
    ``bundle`` explicitly to override (operator switching bundles in the
    UI). Pre-migration analyses without ``bank_name``/``account_last4``
    all share the ``(None, None)`` bundle and behave identically to the
    pre-bundling implementation.
    """
    rows = docs.list_documents(merchant_id=merchant_id, limit=window * 4)
    analyzed: list[tuple[DocumentRow, AnalysisRow]] = []
    for d in rows:
        a = docs.get_analysis(d.id)
        if a is None:
            continue
        analyzed.append((d, a))

    if not analyzed:
        return []

    selected_bundle = bundle if bundle is not None else _select_default_bundle(analyzed)
    if selected_bundle is None:
        return []

    filtered = _filter_to_bundle(analyzed, selected_bundle)
    return filtered[:window]


def _decimal_or_none(value: str) -> Decimal | None:
    """Parse a form-string to Decimal; return None for empty/whitespace."""
    s = value.strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc


def _int_or_none(value: str) -> int | None:
    s = value.strip()
    if not s:
        return None
    return int(s)


def _parse_tiers_json(value: str) -> tuple[FunderTier, ...]:
    """Parse the funder-import form's hidden tiers JSON string.

    Empty string or "[]" → empty tuple. Otherwise must be a JSON array
    of objects, each validated against FunderTier (Pydantic catches
    inverted buy_rates, out-of-range FICO, etc.). Raises ValueError with
    a human-readable message on any malformed input.
    """
    import json as _json

    s = value.strip()
    if not s:
        return ()
    try:
        raw = _json.loads(s)
    except _json.JSONDecodeError as exc:
        raise ValueError(f"tiers field is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"tiers must be a JSON array, got {type(raw).__name__}")
    try:
        return tuple(FunderTier.model_validate(t) for t in raw)
    except ValidationError as exc:
        raise ValueError(f"tier validation failed: {exc}") from exc


def _parse_bullet_lines(value: str) -> tuple[str, ...]:
    """Split a textarea value into bullet entries — one per non-empty line.

    Used for auto_decline_conditions and conditional_requirements where
    each bullet may itself contain commas (so the existing comma-split
    pattern for excluded_industries / excluded_states does not work).
    """
    return tuple(line.strip() for line in value.splitlines() if line.strip())


_TRUE_TOKENS: frozenset[str] = frozenset({"true", "on", "yes", "1"})


def _parse_csv_list(value: str, *, upper: bool = False) -> tuple[str, ...]:
    """Split a comma-separated form value into a tuple of trimmed strings.

    Empties are dropped. ``upper=True`` upper-cases each entry — used for
    state codes (``"ca, ny"`` → ``("CA", "NY")``).
    """
    parts = (s.strip() for s in value.split(","))
    if upper:
        return tuple(s.upper() for s in parts if s)
    return tuple(s for s in parts if s)


_FUNDER_FORM_FIELDS: tuple[str, ...] = (
    "name",
    "active",
    "min_monthly_revenue",
    "min_avg_daily_balance",
    "min_credit_score",
    "min_months_in_business",
    "max_positions",
    "accepts_stacking",
    "min_advance",
    "max_advance",
    "max_nsf_tolerance",
    "requires_coj",
    "typical_factor_low",
    "typical_factor_high",
    "typical_holdback_low",
    "typical_holdback_high",
    "excluded_industries",
    "excluded_states",
    "contact_name",
    "contact_phone",
    "contact_email",
    "submission_email",
    "charges_merchant_advance_fees",
    "aegis_compensation_disclosure_text",
    "operator_notes",
)


def _funder_form_dict_from_locals(locs: dict[str, Any]) -> dict[str, str]:
    """Lift the named funder-form fields out of a route's local namespace.

    Same discipline as ``_form_dict_from_locals`` for merchants — only the
    documented field names pass through, never auxiliary locals (request,
    repo, etc.).
    """
    return {k: str(locs.get(k, "")) for k in _FUNDER_FORM_FIELDS}


def _funder_form_error(
    request: Request,
    error: str,
    form: dict[str, str],
) -> HTMLResponse:
    """Re-render the manual-create form with an error banner and posted values."""
    return templates.TemplateResponse(
        request,
        "funder_form.html.j2",
        {"error": error, "form": form},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _score_input_from_dashboard(
    merchant: MerchantRow,
    document: DocumentRow,
    analysis: AnalysisRow,
) -> ScoreInput:
    """Build a ScoreInput for the matched-funders dashboard panel.

    The match panel needs the same shape ``score_deal`` consumes, but the
    dashboard's "deal" is a derived view (per audit F1) without the
    operator's requested-amount / requested-factor / requested-term-days
    inputs. Use sane defaults: midpoint of the funder typical ranges, 120-
    day term. Operator overrides via the bearer-token API path before any
    real submission ships.
    """
    monthly = _project_monthly(analysis.true_revenue, analysis.statement_days)
    # Same caller-gated contract as score_input_multi_month — see the
    # note in scoring/multi_month.py. Empty string is the documented
    # fallback for callers that lose track of the finalized check.
    return ScoreInput(
        merchant_id=merchant.id,
        business_name=merchant.business_name,
        owner_name=merchant.owner_name,
        state=(merchant.state or "").upper(),
        industry_naics=merchant.industry_naics,
        industry_risk_tier=merchant.industry_risk_tier,
        time_in_business_months=merchant.time_in_business_months,
        credit_score=merchant.credit_score,
        avg_daily_balance=analysis.avg_daily_balance,
        true_revenue=analysis.true_revenue,
        monthly_revenue=monthly,
        lowest_balance=analysis.lowest_balance,
        num_nsf=analysis.num_nsf,
        days_negative=analysis.days_negative,
        mca_positions=analysis.mca_positions,
        mca_daily_total=analysis.mca_daily_total,
        debt_to_revenue=analysis.debt_to_revenue,
        payroll_detected=analysis.payroll_detected,
        returned_ach_count=analysis.returned_ach_count,
        statement_period_start=analysis.statement_period_start,
        statement_period_end=analysis.statement_period_end,
        statement_days=analysis.statement_days,
        fraud_score=document.fraud_score or 0,
        eof_markers=1,
        validation_passed=document.parse_status != "manual_review",
        extraction_confidence=100,
        # Operator-input fields not stored in analysis yet: use placeholders
        # that the funder match doesn't gate on (50K/1.30/120d). Real
        # submissions go through the API where these come from a form.
        requested_amount=Decimal("50000.00"),
        requested_factor=Decimal("1.30"),
        requested_term_days=120,
    )


def _project_monthly(period_revenue: Decimal, statement_days: int) -> Decimal:
    if statement_days <= 0:
        return Decimal("0.00")
    return (period_revenue / Decimal(statement_days) * Decimal(30)).quantize(
        Decimal("0.01")
    )




def _criteria_comparison(
    funder: FunderRow, score_input: ScoreInput
) -> list[dict[str, Any]]:
    """Side-by-side ``funder gate -> deal value`` rows for the merchant_match card.

    Only emits a row when the funder has set the gate (None means "no
    policy"). Status is "fail" / "warn" / "pass" so the template can
    color the row without re-deriving the matcher rule.
    """
    rows: list[dict[str, Any]] = []

    # The comparison values are stringified or filtered at render time;
    # widening to ``object`` keeps mypy strict happy without forcing the
    # caller to pre-coerce numeric / string / None values.
    def add(
        label: str,
        funder_value: object,
        deal_value: object,
        passed: bool,
        *,
        unit: str = "",
        soft: bool = False,
    ) -> None:
        if soft and not passed:
            status_str = "warn"
        elif passed:
            status_str = "pass"
        else:
            status_str = "fail"
        rows.append(
            {
                "label": label,
                "funder_value": funder_value,
                "deal_value": deal_value,
                "status": status_str,
                "unit": unit,
            }
        )

    if funder.min_monthly_revenue is not None:
        add(
            "Minimum monthly revenue",
            funder.min_monthly_revenue,
            score_input.monthly_revenue,
            score_input.monthly_revenue >= funder.min_monthly_revenue,
            unit="money",
        )
    if funder.min_avg_daily_balance is not None:
        add(
            "Minimum average daily balance",
            funder.min_avg_daily_balance,
            score_input.avg_daily_balance,
            score_input.avg_daily_balance >= funder.min_avg_daily_balance,
            unit="money",
        )
    if funder.min_credit_score is not None:
        if score_input.credit_score is None:
            add(
                "Minimum credit score",
                funder.min_credit_score,
                "missing",
                False,
                unit="fico",
                soft=True,
            )
        else:
            add(
                "Minimum credit score",
                funder.min_credit_score,
                score_input.credit_score,
                score_input.credit_score >= funder.min_credit_score,
                unit="fico",
            )
    if funder.min_months_in_business is not None:
        if score_input.time_in_business_months is None:
            add(
                "Minimum time in business",
                funder.min_months_in_business,
                "missing",
                False,
                unit="months",
                soft=True,
            )
        else:
            add(
                "Minimum time in business",
                funder.min_months_in_business,
                score_input.time_in_business_months,
                score_input.time_in_business_months >= funder.min_months_in_business,
                unit="months",
            )
    if funder.max_positions is not None:
        add(
            "Maximum stacked positions",
            funder.max_positions,
            score_input.mca_positions,
            score_input.mca_positions <= funder.max_positions,
            unit="count",
        )
    if not funder.accepts_stacking and score_input.mca_positions > 0:
        add(
            "Stacking acceptance",
            "does not stack",
            f"{score_input.mca_positions} existing position(s)",
            False,
            unit="",
        )
    if funder.max_nsf_tolerance is not None:
        add(
            "Maximum NSF count",
            funder.max_nsf_tolerance,
            score_input.num_nsf,
            score_input.num_nsf <= funder.max_nsf_tolerance,
            unit="count",
        )
    if funder.max_advance is not None:
        add(
            "Maximum advance",
            funder.max_advance,
            score_input.requested_amount,
            score_input.requested_amount <= funder.max_advance,
            unit="money",
        )
    if funder.min_advance is not None:
        add(
            "Minimum advance",
            funder.min_advance,
            score_input.requested_amount,
            score_input.requested_amount >= funder.min_advance,
            unit="money",
        )
    if score_input.industry_naics and funder.excluded_industries:
        excluded = any(
            score_input.industry_naics.startswith(x) for x in funder.excluded_industries
        )
        add(
            "Industry exclusion",
            ", ".join(funder.excluded_industries[:5])
            + ("..." if len(funder.excluded_industries) > 5 else ""),
            score_input.industry_naics,
            not excluded,
            unit="",
        )
    if funder.excluded_states:
        excluded_st = score_input.state in funder.excluded_states
        add(
            "State exclusion",
            ", ".join(funder.excluded_states[:8])
            + ("..." if len(funder.excluded_states) > 8 else ""),
            score_input.state,
            not excluded_st,
            unit="",
        )
    return rows


def _match_card(
    funder: FunderRow,
    match: FunderMatch,
    score_input: ScoreInput | None = None,
) -> dict[str, Any]:
    """Translate a FunderMatch into a card dict the template renders.

    ``match_funder`` sets ``reasons=["tier_<X>"]`` exactly when the
    merchant clears every funder criterion (qualifies=True), and
    ``reasons=[]`` otherwise. We use ``reasons`` as the qualifies
    signal — NOT ``match_score`` — because ``_likelihood`` returns 0
    for both real disqualification AND tier-F merchants who clear
    every criterion (the tier-F base is 0, so
    ``max(0, 0 - 10*len(soft))`` is always 0). Conflating those cases
    rendered every funder card red+disabled for tier-F merchants even
    when they qualified — a known UI bug. When qualified, the
    ``soft_concerns`` list holds soft signals only; when not qualified
    it holds hard-fail reasons unioned with soft (matcher returns
    ``hard + soft`` so the operator sees the full picture).

    Color rule:
      * red    — not qualifies (at least one hard fail on funder criteria)
      * yellow — qualifies with at least one soft concern
      * green  — qualifies and zero soft concerns

    When ``score_input`` is supplied, the card also carries a side-by-side
    criteria comparison rendered as an expandable details block inside the
    funder card.
    """
    qualifies = bool(match.reasons)
    if not qualifies:
        color = "red"
        hard_reasons = list(match.soft_concerns)
        soft_concerns: list[str] = []
    elif match.soft_concerns:
        color = "yellow"
        hard_reasons = []
        soft_concerns = list(match.soft_concerns)
    else:
        color = "green"
        hard_reasons = []
        soft_concerns = []

    criteria: list[dict[str, Any]] = []
    if score_input is not None:
        criteria = _criteria_comparison(funder, score_input)

    return {
        "funder_id": str(funder.id),
        "funder_name": funder.name,
        "match_score": match.match_score,
        "color": color,
        "hard_reasons": hard_reasons,
        "soft_concerns": soft_concerns,
        "criteria_comparison": criteria,
        "funder_requires_coj": funder.requires_coj,
        "funder_charges_merchant_advance_fees": funder.charges_merchant_advance_fees,
        # Per-funder pricing guidance (R4.2 + R4.3 EstimatedTerms).
        # ``None`` when the funder has no pricing envelope or the score
        # tier falls outside the interpolation table — template suppresses
        # the pricing block in that case (no empty row, no placeholders).
        "estimated_terms": match.estimated_terms,
    }


@dataclass
class _UploadResult:
    """Per-file outcome surfaced to the operator on the upload form."""

    filename: str
    status: str  # "ok" | "duplicate" | "error"
    document_id: str | None
    detail: str  # human-readable summary or error message


async def _persist_uploads(
    *,
    request: Request,
    files: list[UploadFile],
    repository: DocumentRepository,
    audit: AuditLog,
    actor: str,
    actor_email: str | None = None,
    merchant_id: UUID | None,
    per_file_cap: int,
    total_cap: int,
) -> tuple[list[_UploadResult], str | None]:
    """Read N files, persist each via ``persist_pdf_upload``, return per-file
    outcomes plus an optional batch-level error.

    Per-file failures (oversize, non-PDF, dedup-race) become an entry in
    the result list with ``status="error"``; the batch keeps going so a
    bad file doesn't kill 3 good ones. A batch-level error (total cap
    exceeded) short-circuits and returns no results.
    """
    # Lazy import — see module-top comment for the cycle this avoids.
    from aegis.api.routes.upload import (
        _make_request_enqueue,
        persist_pdf_upload,
    )

    bodies: list[tuple[str, bytes]] = []
    running_total = 0
    for f in files:
        body = await f.read(per_file_cap + 1)
        if len(body) > per_file_cap:
            return (
                [],
                f"{f.filename or 'unnamed'} exceeds the per-file cap of {per_file_cap} bytes",
            )
        running_total += len(body)
        if running_total > total_cap:
            return (
                [],
                f"total upload size exceeds the {total_cap}-byte batch cap",
            )
        bodies.append((f.filename or "unnamed.pdf", body))

    results: list[_UploadResult] = []
    for filename, body in bodies:
        try:
            resp = await persist_pdf_upload(
                enqueue_parse=_make_request_enqueue(request),
                body=body,
                original_filename=filename,
                repository=repository,
                audit=audit,
                actor=actor,
                actor_email=actor_email,
                merchant_id=merchant_id,
            )
        except HTTPException as exc:
            results.append(
                _UploadResult(
                    filename=filename,
                    status="error",
                    document_id=None,
                    detail=str(exc.detail),
                )
            )
            continue
        results.append(
            _UploadResult(
                filename=filename,
                status="duplicate" if resp.duplicate_of_existing else "ok",
                document_id=str(resp.document_id),
                detail=(
                    "deduped to existing document"
                    if resp.duplicate_of_existing
                    else f"queued (parse_status={resp.parse_status})"
                ),
            )
        )
    return results, None


def _intake_form_error(
    request: Request, error: str, form: dict[str, Any]
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "intake.html.j2",
        {"error": error, "form": form},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _validate_merchant_state(state: str) -> str | None:
    """Return an error string if the state isn't served, else None."""
    try:
        validate_state_served(state.upper())
    except StateNotServed as exc:
        return str(exc)
    return None


_FORM_FIELDS: tuple[str, ...] = (
    "business_name",
    "owner_name",
    "state",
    "dba",
    "industry_naics",
    "credit_score",
    "time_in_business_months",
    "email",
    "phone",
    "entity_type",
    "ein",
    "requested_amount",
    "requested_factor",
    "requested_term_days",
    "broker_source",
    "intake_date",
    "is_renewal",
)


def _form_dict_from_locals(locs: dict[str, Any]) -> dict[str, str]:
    """Lift the named form fields out of a route's local namespace.

    Keeps the form re-render path strict: only the documented field names
    pass through, never auxiliary locals (request, repo, etc.) that would
    leak into the template context.
    """
    return {k: str(locs.get(k, "")) for k in _FORM_FIELDS}


def _merchant_form_error(
    request: Request,
    error: str,
    form: dict[str, str],
    *,
    merchant: MerchantRow | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "merchant_form.html.j2",
        {"merchant": merchant, "error": error, "form": form},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _entity_type_or_none(value: str) -> EntityType | None:
    """Coerce a form-string to ``EntityType`` or ``None``.

    Strict-cast: anything outside the literal set returns ``None`` so a
    mistyped entity_type doesn't crash the intake flow. Callers pass
    user input directly from the form, where the ``<select>`` constrains
    valid values, but defense-in-depth is cheap here.
    """
    v = value.strip().lower()
    if v in {"llc", "corp", "sole_prop", "partnership", "other"}:
        return cast(EntityType, v)
    return None


def _state_tier(state: str | None) -> int | str:
    """Resolve the state regulation tier for the ribbon.

    Returns 1/2/3 for served states; ``"unserved"`` if the state isn't
    in the served set; ``"unknown"`` if ``state`` itself is ``None``.

    Pure read of ``STATES`` — no side effects.

    The ``None`` case lands when an auto-finalized merchant has no
    state yet (parser doesn't extract address; operator sets state via
    edit). ``"unknown"`` is distinct from ``"unserved"`` — unserved is a
    real state AEGIS chose not to fund in; unknown is "we don't yet
    know what state this merchant is in." The dossier renders both as
    "—" but downstream code (compliance / scoring) can distinguish.
    """
    if state is None:
        return "unknown"
    reg = STATES.get(state.upper())
    if reg is None:
        return "unserved"
    return int(reg.tier)


def _ofac_ribbon_status(
    ofac: OFACClient | None, business_name: str
) -> tuple[str, bool | None]:
    """Best-effort OFAC indicator for the ribbon.

    Returns a (status, match) tuple where status is one of:
      * ``"checked"``  — query succeeded; ``match`` carries the boolean
      * ``"stale"``    — cache was stale and refresh failed
      * ``"unavailable"`` — query raised something else (treated as no info)
      * ``"not_consulted"`` — no client wired (dev/offline)
    """
    if ofac is None:
        return ("not_consulted", None)
    try:
        return ("checked", ofac.is_match(business_name))
    except OFACStaleError:
        return ("stale", None)
    except Exception:
        return ("unavailable", None)


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


def _txs_to_rows(txs: list[ClassifiedTransaction]) -> list[dict[str, Any]]:
    """Stable display ordering by posted_date, then page/line."""
    return [
        {
            "posted_date": t.posted_date.isoformat(),
            "description": t.description,
            "amount": str(t.amount),
            "running_balance": str(t.running_balance) if t.running_balance else "",
            "category": t.category,
            "source_page": t.source_page,
            "source_line": t.source_line,
        }
        for t in sorted(txs, key=lambda t: (t.posted_date, t.source_page, t.source_line))
    ]


# ---------------------------------------------------------------------------
# Phase 10 — operator override capture (mp §20).
# ---------------------------------------------------------------------------
#
# Operator clicks "I disagree" on the dossier, picks a reason code +
# (optionally) typed-in pattern false-positives, and AEGIS persists an
# ``overrides`` row tied to ``decision_id``. Outcome stamping lives on
# the funder_replies side (refinement 5); ``record_override`` back-
# stamps from any pending reply at creation time.
#
# The /ui surface is gated by Cloudflare Access in production (not
# require_bearer), matching the rest of this router.


@router.post(
    "/decisions/{decision_id}/override",
    response_model=None,
    include_in_schema=False,
)
async def decision_override(
    decision_id: UUID,
    audit: Annotated[AuditLog, Depends(get_audit)],
    override_repo: Annotated[OverrideRepository, Depends(get_override_repository)],
    reply_repo: Annotated[FunderReplyRepository, Depends(get_funder_reply_repository)],
    deal_id: Annotated[UUID, Form()],
    original_recommendation: Annotated[str, Form()],
    operator_decision: Annotated[str, Form()],
    reason_code: Annotated[str, Form()],
    reason_detail: Annotated[str, Form()] = "",
    pattern_false_positive: Annotated[str, Form()] = "",
) -> JSONResponse:
    """Persist one operator override + back-stamp from pending replies.

    ``pattern_false_positive`` is a comma-separated list of detector
    codes (the modal renders the active detectors as checkboxes and
    serializes the selection into one form field). Empty entries are
    dropped so a blank submit doesn't write ``[""]`` to the array
    column.
    """
    patterns = [p.strip() for p in pattern_false_positive.split(",") if p.strip()]
    try:
        payload = OverridePayload(
            deal_id=deal_id,
            decision_id=decision_id,
            original_recommendation=original_recommendation,
            operator_decision=operator_decision,
            reason_code=reason_code,
            reason_detail=reason_detail.strip() or None,
            pattern_false_positive=patterns,
            operator_id="dashboard",
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid override payload: {exc}",
        ) from exc

    try:
        result = record_override(
            payload,
            repo=override_repo,
            reply_repo=reply_repo,
            audit=audit,
        )
    except OverrideError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"override_persist_unavailable: {exc}",
        ) from exc

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "override_id": str(result.override_id),
            "back_stamped_outcome": result.back_stamped_outcome,
        },
    )


# ---------------------------------------------------------------------------
# /compliance/obligations — registration deadlines dashboard (mp Phase 7).
# ---------------------------------------------------------------------------


@router.get("/compliance/obligations", response_class=HTMLResponse)
async def compliance_obligations(request: Request) -> HTMLResponse:
    """Operator view of state registration / annual-report obligations.

    Reads from `compliance_obligations` (migration 018). Rows are annotated
    in-Python with a `derived_state` (overdue / due_soon / on_track) so
    the template stays date-math-free.
    """
    from aegis.compliance.obligations import (
        get_obligations_repository,
        summarize,
    )

    repo = get_obligations_repository()
    rows = repo.list_obligations()
    summary = summarize(rows)

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "compliance_obligations.html.j2",
            {
                "active": "Compliance",
                "obligations": rows,
                "summary": summary,
            },
        ),
    )


# ---------------------------------------------------------------------------
# /renewals — upcoming-maturity calendar (R3.2, operator-visibility only).
#
# Per ``.claude/rules/compliance.md`` SCOPE NOTE: AEGIS does not own
# regulator-facing renewal disclosure issuance — funder partners do
# (CA SB 362 § 22806 — 60 days pre-maturity; NY 23 NYCRR § 600.17 —
# 30 days pre-maturity). This route is an operator-visibility surface
# so the operator can verify the funder has transmitted the required
# pre-maturity notice. The list is NOT used to drive any broker-side
# enforcement gate.
# ---------------------------------------------------------------------------


_RENEWAL_WINDOW_DEFAULT_DAYS: Final[int] = 90
_RENEWAL_WINDOW_MAX_DAYS: Final[int] = 365


@router.get("/renewals", response_class=HTMLResponse)
async def upcoming_renewals(
    request: Request,
    merchants_repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    attestations_repo: Annotated[
        RenewalAttestationRepository,
        Depends(get_renewal_attestation_repository),
    ],
    window_days: Annotated[
        int,
        Query(
            ge=1,
            le=_RENEWAL_WINDOW_MAX_DAYS,
            description="Lookahead window in days; default 90.",
        ),
    ] = _RENEWAL_WINDOW_DEFAULT_DAYS,
    flash: Annotated[
        str | None,
        Query(
            description=(
                "Optional flash message rendered above the table after a "
                "successful POST /ui/renewals/{merchant_id}/attest redirect."
            ),
        ),
    ] = None,
) -> HTMLResponse:
    """Operator-visibility calendar of merchants approaching maturity.

    Rows derive from ``MerchantRepository.list_all()`` filtered to
    ``is_renewal=True`` whose ``maturity_date`` falls within
    ``window_days``. Sorted by ``days_until_maturity`` ascending (most
    urgent first). When the ``maturity_date`` column is absent from the
    schema (current state — see ``list_upcoming_renewals`` docstring),
    the accessor returns an empty list and the template renders an
    explicit "schema augmentation pending" empty state instead of a
    misleading "no rows" message.

    The ``attestations_repo`` consumes ``funder_renewal_attestations``
    (migration 040 / U6) to flip per-row ``renewal_status`` off the
    default ``not_required_funder_owns`` when the operator has captured
    an attestation that the funder transmitted the required notice.
    """
    rows = list_upcoming_renewals(
        merchants_repo, window_days=window_days, attestations=attestations_repo
    )
    # Detect the schema-augmentation gap so the template can render a
    # different empty state than the legitimate "no merchants in window"
    # case. Mirrors the accessor's own gap-detection: if any merchant in
    # the repo carries a real ``maturity_date`` attribute, the schema is
    # present and the empty result truly means "nobody's maturing soon."
    schema_missing = not any(
        isinstance(getattr(m, "maturity_date", None), date)
        and not isinstance(getattr(m, "maturity_date", None), datetime)
        for m in merchants_repo.list_all()
    )
    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "renewals.html.j2",
            {
                "active": "Renewals",
                "rows": rows,
                "window_days": window_days,
                "schema_missing": schema_missing,
                "flash": flash,
            },
        ),
    )


# ---------------------------------------------------------------------------
# /renewals/{merchant_id}/attest — operator captures a funder attestation
# that the required pre-maturity disclosure was transmitted (U6).
#
# Per CLAUDE.md SCOPE NOTE + ``.claude/rules/compliance.md`` SCOPE NOTE:
# AEGIS does NOT own the regulator-facing disclosure obligation. The row
# this route writes records an OPERATOR CLAIM about the funder's
# behavior; it is not itself a regulator-facing audit artifact. The
# funder's own audit trail remains the regulator-facing record.
#
# Idempotency policy: duplicate (merchant_id, maturity_date, funder_name)
# attestations return 409 rather than silently coalescing. Rationale
# documented on RenewalAttestationConflictError.
# ---------------------------------------------------------------------------


_RENEWAL_ATTESTATION_NOTES_MAX_LEN: Final[int] = 2000


@router.post("/renewals/{merchant_id}/attest", response_model=None)
async def renewal_attestation_submit(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    attestations: Annotated[
        RenewalAttestationRepository,
        Depends(get_renewal_attestation_repository),
    ],
    audit: Annotated[AuditLog, Depends(get_audit)],
    funder_name: Annotated[str, Form()],
    disclosure_sent_at: Annotated[str, Form()],
    maturity_date_form: Annotated[str, Form(alias="maturity_date")],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
    notes: Annotated[str, Form()] = "",
) -> Response:
    """Record one operator attestation that the funder sent the renewal notice.

    The merchant must exist + be a finalized renewal row with the same
    ``maturity_date`` as the one the operator is attesting against. The
    state and statute are derived from the merchant + the lookup table
    in ``aegis.merchants.renewal_attestations`` — the operator does NOT
    enter them so the row is consistent with what the calendar surfaced.

    Returns a 303 redirect back to ``/ui/renewals`` with a flash message
    on success, a 400 on bad input, a 404 on unknown merchant, and a
    409 when an attestation for the same (merchant, maturity, funder)
    already exists (see ``RenewalAttestationConflictError`` rationale).
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    if not merchant.is_renewal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant is not flagged as a renewal",
        )
    if merchant.maturity_date is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no maturity_date — set it before attesting",
        )
    if merchant.state is None or len(merchant.state) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no state — set it before attesting",
        )

    # Parse + validate the date inputs. The form supplies maturity_date
    # back so the route can verify the operator is attesting against the
    # maturity they saw on the calendar (defensive against a stale form
    # render after the operator edited the merchant in another tab).
    try:
        parsed_maturity = date.fromisoformat(maturity_date_form.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid maturity_date: {maturity_date_form!r}",
        ) from exc
    if parsed_maturity != merchant.maturity_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "submitted maturity_date does not match merchant maturity_date "
                "— reload the renewal calendar and retry"
            ),
        )

    try:
        parsed_sent_at = date.fromisoformat(disclosure_sent_at.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid disclosure_sent_at: {disclosure_sent_at!r}",
        ) from exc

    cleaned_funder = funder_name.strip()
    if not cleaned_funder:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="funder_name must not be empty",
        )
    if len(cleaned_funder) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="funder_name exceeds 255 characters",
        )

    cleaned_notes = notes.strip()
    if len(cleaned_notes) > _RENEWAL_ATTESTATION_NOTES_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"notes exceeds {_RENEWAL_ATTESTATION_NOTES_MAX_LEN} characters"
            ),
        )

    try:
        record_renewal_attestation(
            attestations,
            audit,
            merchant_id=merchant.id,
            funder_name=cleaned_funder,
            maturity_date=merchant.maturity_date,
            disclosure_sent_at=parsed_sent_at,
            attested_by=actor_email or "dashboard",
            state=merchant.state,
            actor_email=actor_email,
            notes=cleaned_notes or None,
        )
    except RenewalAttestationConflictError as exc:
        # 409: duplicate attestation. The operator sees the conflict
        # message in the rendered HTTPException response; the row is
        # NOT written and no audit entry is recorded.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except RenewalAttestationWriteError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    flash_msg = (
        f"Recorded {cleaned_funder} attestation for {parsed_sent_at.isoformat()}."
    )
    return RedirectResponse(
        url=f"/ui/renewals?flash={urllib.parse.quote(flash_msg)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# /portfolio — operator analytics over the deal pipeline (M11 / U11)
#
# Read-only aggregations across merchants, documents, audit_log, and
# funder_replies. The math lives in
# ``aegis.deals.portfolio_analytics.compute_portfolio_metrics``; this
# handler is the I/O layer that pulls the rows in the requested date
# window and hands them to the pure aggregator.
#
# Auth: same Cloudflare-Access posture as every other ``/ui/...`` route —
# no bearer required; the SSO gate sits in front of the app in prod and
# is bypassed on localhost dev.
#
# PII: business_name renders in the per-deal recent-activity table per
# the existing dashboard pattern (merchants table shows business names
# today). It is NEVER written to a query-string or audit row from this
# route — the route reads only.
# ---------------------------------------------------------------------------


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
                .select(
                    "id,deal_id,decided_at,decision,state_code,"
                    "score,score_factors"
                )
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

            get_logger(__name__).warning("portfolio.fetch_failed window=%s..%s",
                date_range.from_date, date_range.to_date)
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
    from_: Annotated[
        str | None,
        Query(
            alias="from",
            description=(
                "Window start (YYYY-MM-DD). Defaults to today minus 30 days."
            ),
        ),
    ] = None,
    to: Annotated[
        str | None,
        Query(
            description=(
                "Window end (YYYY-MM-DD). Defaults to today."
            ),
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

    audit_rows, reply_rows, decision_rows = _fetch_portfolio_data(
        audit, snapshot, date_range
    )

    metrics = compute_portfolio_metrics(
        merchants=merchants_list,
        funders=funders_list,
        documents=documents_list,
        funder_reply_rows=reply_rows,
        audit_rows=audit_rows,
        date_range=date_range,
        decision_rows=decision_rows,
    )

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "portfolio.html.j2",
            {
                "active": "Portfolio",
                "metrics": metrics,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )


__all__ = ["router"]
