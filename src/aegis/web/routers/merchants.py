"""Merchants sub-router — list / new / detail dossier / edit / match / submit.

Routes:
  * ``GET  /ui/merchants``                              — list all merchants
  * ``GET  /ui/merchants/new``                          — manual create form
  * ``POST /ui/merchants/new``                          — create merchant
  * ``GET  /ui/merchants/{merchant_id}``                — detail dossier (HTML)
  * ``GET  /ui/merchants/{merchant_id}/edit``           — edit form
  * ``POST /ui/merchants/{merchant_id}/edit``           — save edit
  * ``GET  /ui/merchants/{merchant_id}/dossier.pdf``    — print PDF
  * ``GET  /ui/merchants/{merchant_id}/findings.csv``   — CSV findings download
  * ``GET  /ui/merchants/{merchant_id}/match``          — matched-funders panel
  * ``POST /ui/merchants/{merchant_id}/submit``         — submit-to-funders (CSV/ZIP)
  * ``POST /ui/merchants/{merchant_id}/funder-response``— funder reply capture
  * ``POST /ui/merchants/{merchant_id}/close-rescan``   — Close attachment rescan

Extracted from ``router.py`` during R4.1 finish-part-4 (the final
domain split). The bundling helpers + ``_collect_analyzed_for_merchant``
+ ``_score_input_from_dashboard`` + ``_project_monthly`` were lifted to
``aegis.web._router_helpers`` so the dashboard sub-router and the
``aegis.api.routes.findings`` integration can drop their lazy back-imports.
Several merchant-private helpers (``_state_tier``, ``_ofac_ribbon_status``,
``_match_card``, ``_dossier_pattern_analysis``) are re-exported from
``aegis.web.router`` so existing test imports keep their paths.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from aegis.api.deps import (
    get_audit,
    get_close_client,
    get_decision_snapshot,
    get_funder_note_submission_repository,
    get_funder_repository,
    get_merchant_repository,
    get_merchant_shadow_signal_repository,
    get_ofac_client,
    get_pdf_store_repository,
    get_repository,
    get_submission_repository,
)
from aegis.audit import AuditLog
from aegis.close.client import CloseClient, CloseError
from aegis.close.funder_note import RenewalContext, format_funder_note
from aegis.close.orchestration import enqueue_close_orchestration
from aegis.compliance.snapshot import (
    DecisionSnapshot,
    InMemoryDecisionSnapshot,
)
from aegis.compliance.states import STATES
from aegis.funder_note_submissions import (
    FunderNoteSubmissionRepository,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import (
    FunderNotFoundError,
    FunderRepository,
)
from aegis.merchants.models import MERCHANT_NOTE_MAX_CHARS, MerchantRow
from aegis.merchants.repository import (
    MerchantConflictError,
    MerchantNotFoundError,
    MerchantRepository,
)
from aegis.merchants.shadow_signals import (
    MerchantShadowSignalRecord,
    MerchantShadowSignalRepository,
)
from aegis.ops.operators import resolve_operator_email
from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import (
    PatternAnalysis,
    analyze_patterns,
    pattern_analysis_from_dto,
)
from aegis.pdf_store import (
    CorruptCiphertextError,
    PdfStoreIntegrityError,
    PdfStoreNotFoundError,
    PdfStoreRepository,
    PdfStoreWriteError,
)
from aegis.scoring.historical_approval import (
    LOOKBACK_DAYS as _HISTORICAL_LOOKBACK_DAYS,
)
from aegis.scoring.historical_approval import (
    build_historical_approval_index,
    lookup_historical_approval_rate,
)
from aegis.scoring.match_funders import match_funder
from aegis.scoring.models import FunderMatch, ScoreInput, ScoreResult
from aegis.scoring.multi_month import (
    detect_missing_months as _detect_missing_months,
)
from aegis.scoring.multi_month import (
    score_input_multi_month as _score_input_multi_month,
)
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
from aegis.scoring.submission_package import build_submission_files
from aegis.scoring_v2.balance_health import compute_balance_health
from aegis.scoring_v2.industry import IndustryTier, industry_risk_tier
from aegis.scoring_v2.mca_stack import aggregate_mca_stack
from aegis.scoring_v2.offer import OfferRecommendation, compute_offer
from aegis.scoring_v2.score_deal_inputs import compute_score_deal_track_inputs
from aegis.scoring_v2.score_for_sync import (
    recommended_factor_rate_from as _recommended_factor_rate_from,
)
from aegis.scoring_v2.stips import StipsResult, evaluate_stips
from aegis.scoring_v2.track_a import IntegrityVerdict
from aegis.scoring_v2.trends import compute_revenue_trends
from aegis.storage import (
    AnalysisRow,
    DocumentNotFoundError,
    DocumentRepository,
    DocumentRow,
)
from aegis.submissions import (
    SubmissionConflictError,
    SubmissionRepository,
    SubmissionWriteError,
    record_submission,
)
from aegis.web._flag_labels import HumanFlag, humanize_flag
from aegis.web._pattern_cards import (
    build_pattern_cards,
    pattern_has_customer_concentration,
)
from aegis.web._router_helpers import (
    _AGGREGATE_LABELS,
    _AGGREGATE_UNIT_KIND,
    _build_bundle_summaries,
    _bundle_keys_for_merchant,
    _collect_analyzed_for_merchant,
    _decimal_or_none,
    _entity_type_or_none,
    _form_dict_from_locals,
    _int_or_none,
    _parse_bundle_query,
    _record_decision_for_score,
    _select_default_bundle,
    _sha256_hex,
    _validate_merchant_state,
)
from aegis.web._slug import slugify
from aegis.web._soft_signals import parse_soft_signal_flags
from aegis.web._stacking_card import build_stacking_card
from aegis.web._templates import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# List / create / edit
# ---------------------------------------------------------------------------


@router.get("/merchants", response_class=HTMLResponse)
async def list_merchants(
    request: Request,
    repo: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    return templates.TemplateResponse(request, "merchants.html.j2", {"merchants": repo.list_all()})


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


# ---------------------------------------------------------------------------
# Matched-funders panel (Phase 7B).
# ---------------------------------------------------------------------------


def _build_historical_index_for_match(
    *,
    funder_note_subs: FunderNoteSubmissionRepository,
    merchants_repo: MerchantRepository,
    snapshot: DecisionSnapshot,
    documents: DocumentRepository,
) -> dict[UUID, dict[tuple[IndustryTier, str], Decimal]]:
    """Load the inputs the matcher's Sprint-4 boost needs and project
    them through ``build_historical_approval_index``.

    Three reads, all cheap relative to the funder-iteration that
    follows (which already touches every active funder):

      * ``funder_note_subs.list_in_window`` -- 90 days of submissions.
      * ``merchants_repo.list_all`` -- one row per merchant for the
        industry_choice map.
      * ``snapshot`` rows -- in-memory branch reads ``snapshot.rows()``
        directly; the Supabase branch reads the ``decisions`` table.
        ``documents`` is consulted only to map ``decisions.deal_id``
        back to the merchant.

    Any exception in the I/O layer collapses to an empty index so the
    /match panel never 500s on a transient outage -- the matcher then
    behaves identically to pre-Sprint-4.
    """
    from aegis.logger import get_logger

    empty: dict[UUID, dict[tuple[IndustryTier, str], Decimal]] = {}

    now_dt = datetime.now(UTC)
    try:
        submissions = funder_note_subs.list_in_window(
            from_dt=now_dt - timedelta(days=_HISTORICAL_LOOKBACK_DAYS),
            to_dt=now_dt,
        )
    except Exception:
        get_logger(__name__).warning("match.historical_submissions_fetch_failed", exc_info=True)
        return empty

    if not submissions:
        return empty

    try:
        all_merchants = merchants_repo.list_all()
    except Exception:
        get_logger(__name__).warning("match.historical_merchants_fetch_failed", exc_info=True)
        return empty

    industry_choice_by_merchant: dict[UUID, str | None] = {
        m.id: m.industry_choice for m in all_merchants
    }

    score_tier_by_merchant: dict[UUID, str] = _load_latest_score_tier_per_merchant(
        snapshot=snapshot,
        documents=documents,
    )

    return build_historical_approval_index(
        submissions=submissions,
        industry_choice_by_merchant=industry_choice_by_merchant,
        score_tier_by_merchant=score_tier_by_merchant,
        now=now_dt,
    )


def _load_latest_score_tier_per_merchant(
    *,
    snapshot: DecisionSnapshot,
    documents: DocumentRepository,
) -> dict[UUID, str]:
    """Merchant -> letter tier of the merchant's most-recent decision.

    Mirrors the projection in
    ``aegis.web.routers.portfolio.portfolio_view`` so the historical-
    approval index and the portfolio dashboard agree on a merchant's
    score tier. Returns ``{}`` on any I/O failure -- the matcher then
    reads ``"unknown"`` per missing merchant, which the operator-spec
    cell-keying treats as no signal.
    """
    from aegis.logger import get_logger

    deal_to_merchant: dict[str, UUID] = {}
    try:
        for d in documents.list_documents(limit=5000):
            if d.merchant_id is not None:
                deal_to_merchant[str(d.id)] = d.merchant_id
    except Exception:
        get_logger(__name__).warning("match.historical_documents_fetch_failed", exc_info=True)
        return {}

    rows: list[dict[str, Any]] = []
    if isinstance(snapshot, InMemoryDecisionSnapshot):
        rows = list(snapshot.rows())
    else:
        try:
            from aegis.db import get_supabase

            result = (
                get_supabase()
                .table("decisions")
                .select("deal_id,decided_at,score_factors")
                .order("decided_at", desc=True)
                .limit(5000)
                .execute()
            )
            rows = cast(list[dict[str, Any]], result.data or [])
        except Exception:
            get_logger(__name__).warning("match.historical_decisions_fetch_failed", exc_info=True)
            return {}

    latest_at: dict[UUID, str] = {}
    latest_tier: dict[UUID, str] = {}
    for row in rows:
        deal_id = str(row.get("deal_id") or "")
        merchant_id = deal_to_merchant.get(deal_id)
        if merchant_id is None:
            continue
        factors = row.get("score_factors")
        if not isinstance(factors, dict):
            continue
        tier = factors.get("tier")
        if not isinstance(tier, str):
            continue
        decided_at = str(row.get("decided_at") or "")
        if decided_at >= latest_at.get(merchant_id, ""):
            latest_at[merchant_id] = decided_at
            latest_tier[merchant_id] = tier
    return latest_tier


_FINTECH_BANK_FLAG_PREFIX = "[WARN] fintech_bank_detected:"


def _fintech_bank_warning_from_flags(all_flags: list[str] | None) -> str | None:
    """Extract the per-merchant fintech-bank warning text from the
    parser's ``all_flags`` so it can be attached to every funder match.

    The parser writes one entry of the form
    ``"[WARN] fintech_bank_detected: <Name> — <reason>"`` when the
    extracted bank_name matches a known fintech / neobank (see
    ``aegis.parser.fintech_banks``). We strip the prefix and rephrase
    into the dossier-facing soft-concern copy the operator wants on
    every funder card. Returns ``None`` when no fintech flag is
    present, which is the common case.
    """
    if not all_flags:
        return None
    for raw in all_flags:
        if raw.startswith(_FINTECH_BANK_FLAG_PREFIX):
            # Format: "[WARN] fintech_bank_detected: Mercury — many ..."
            tail = raw[len(_FINTECH_BANK_FLAG_PREFIX) :].strip()
            canonical_name = tail.split("—", 1)[0].strip()
            if canonical_name:
                return (
                    f"Merchant banks with {canonical_name}. Verify funder "
                    f"accepts fintech bank accounts before submitting."
                )
    return None


def _build_match_cards(
    *,
    merchant: MerchantRow,
    score_input: ScoreInput,
    score_result: ScoreResult,
    funder_repo: FunderRepository,
    merchants_repo: MerchantRepository,
    docs: DocumentRepository,
    funder_note_subs: FunderNoteSubmissionRepository,
    snapshot: DecisionSnapshot,
    offer: OfferRecommendation | None = None,
    bank_warning: str | None = None,
) -> list[dict[str, Any]]:
    """Run the per-funder matcher and return ranked card dicts.

    Shared between the standalone ``/match`` panel and the dossier's
    inline § 4 funder-matching section so the two surfaces stay in lockstep
    (same color rule, same historical-approval boost, same sort order). On
    a historical-index outage the matcher silently falls back to the
    pre-Sprint-4 path -- the panel never 500s.

    ``offer`` (2026-06-24 offer-sizing wire-through): when supplied,
    threaded through ``match_funder`` so per-funder pricing and per-tier
    sizing seed from ``offer.recommended_amount`` instead of the legacy
    ``score.suggested_max_advance``. Callers that don't have an offer
    handy can omit; behaviour is unchanged.

    ``bank_warning`` is the parser-emitted fintech-bank warning text
    (when the merchant banks with Mercury / Brex / Novo / etc.). When
    supplied, it lands as a soft concern on every returned card so the
    operator sees the same caveat regardless of which funder they're
    eyeing. See ``_fintech_bank_warning_from_flags`` for how callers
    derive the string from ``latest_doc.all_flags``.
    """
    historical_index = _build_historical_index_for_match(
        funder_note_subs=funder_note_subs,
        merchants_repo=merchants_repo,
        snapshot=snapshot,
        documents=docs,
    )
    deal_industry_tier = industry_risk_tier(merchant.industry_choice)
    deal_score_tier = score_result.tier
    cards: list[dict[str, Any]] = []
    for funder in funder_repo.list_active():
        historical_rate = lookup_historical_approval_rate(
            historical_index,
            funder_id=funder.id,
            industry_tier=deal_industry_tier,
            score_tier=deal_score_tier,
        )
        m = match_funder(
            funder,
            score_input,
            score_result,
            historical_approval_rate=historical_rate,
            merchant=merchant,
            offer=offer,
            bank_warning=bank_warning,
        )
        if m is None:
            continue
        cards.append(_match_card(funder, m, score_input))
    cards.sort(key=lambda c: c["match_score"], reverse=True)
    return cards


@router.get("/merchants/{merchant_id}/match", response_class=HTMLResponse)
async def merchant_match(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
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
    # U33 — Track A/B feed for the matched-funders panel. ``items`` is the
    # bundle-filtered (doc, analysis) tuple list — same shape the score
    # input was built from. Reusing it keeps the Track A/B inputs aligned
    # with the score window the operator sees.
    _match_documents = [d for d, _ in items]
    _match_analyses_by_doc = {d.id: a for d, a in items}
    track_a_verdict, track_b_band = compute_score_deal_track_inputs(
        documents=_match_documents,
        list_transactions=docs.list_transactions,
        analyses_by_doc=_match_analyses_by_doc,
        merchant_id=merchant_id,
    )
    try:
        score_result = score_deal(
            score_input,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    # 2026-06-24 offer-sizing wire-through: feed the funder match grid
    # from the same OfferRecommendation the dossier surfaces (capacity-
    # aware + stack-overload-discounted). The grid previously seeded
    # off ``score.suggested_max_advance`` (crude tier x revenue) — a
    # known mismatch with the dossier offer chip.
    _match_latest_doc, _match_latest_analysis = items[0]
    _match_latest_txns = docs.list_transactions(_match_latest_doc.id)
    _match_mca_stack = aggregate_mca_stack(
        transactions=_match_latest_txns,
        monthly_revenue=_match_latest_analysis.monthly_revenue,
        period_days=_match_latest_analysis.statement_days,
    )
    offer = compute_offer(
        true_revenue_monthly=_match_latest_analysis.monthly_revenue,
        holdback_capacity_monthly=(_match_latest_analysis.monthly_revenue * Decimal("0.25")),
        mca_stack=_match_mca_stack,
    )

    cards = _build_match_cards(
        merchant=merchant,
        score_input=score_input,
        score_result=score_result,
        funder_repo=funder_repo,
        merchants_repo=merchants,
        docs=docs,
        funder_note_subs=funder_note_subs,
        snapshot=snapshot,
        offer=offer,
        bank_warning=_fintech_bank_warning_from_flags(
            list(_match_latest_doc.all_flags) if _match_latest_doc is not None else None
        ),
    )

    # Preselect: pre-check the matching funder's checkbox unless the
    # funder is hard-failing this merchant (color=red), in which case we
    # surface a banner with the reasons and leave the checkbox unchecked.
    preselect_id_str: str | None = None
    preselect_banner: dict[str, Any] | None = None
    if preselect_funder is not None:
        target_id = str(preselect_funder)
        matched_card = next((c for c in cards if c["funder_id"] == target_id), None)
        if matched_card is None:
            try:
                f = funder_repo.get(preselect_funder)
                preselect_banner = {
                    "name": f.name,
                    "reasons": [
                        "This funder is not active or has no matchable criteria for this merchant."
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
        "any_manual_review": any(d.parse_status == "manual_review" for d, _ in items),
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


# ---------------------------------------------------------------------------
# Matched-funders CSV download — paper-trail snapshot for the inline
# dossier § 4 panel (Phase 7B inline-match wave). Distinct from
# ``/submit`` below: that route builds per-funder application packages
# the operator forwards to funders; this route is a single CSV summarising
# which funders were considered for this deal + their match scores +
# qualifying status. Used by the operator team when auditing routing
# decisions after the fact.
# ---------------------------------------------------------------------------


@router.get("/merchants/{merchant_id}/matched-funders.csv", response_model=None)
async def merchant_matched_funders_csv(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
) -> Response:
    """Download a single CSV summarising every matched funder for this deal.

    Header block carries the deal-level snapshot (business name, score,
    tier, suggested-offer envelope) followed by a blank row, then a
    table block with one row per matched funder + their match score,
    color, hard fails, soft concerns, and estimated terms. The same
    cards the inline § 4 dossier panel renders.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not merchant.is_finalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant is not finalized — name the merchant before downloading match CSV",
        )

    items = _collect_analyzed_for_merchant(docs, merchant_id)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no analyzed document — upload + parse first",
        )

    score_input = _score_input_multi_month(merchant, items)
    csv_documents = [d for d, _ in items]
    csv_analyses_by_doc = {d.id: a for d, a in items}
    track_a_verdict, track_b_band = compute_score_deal_track_inputs(
        documents=csv_documents,
        list_transactions=docs.list_transactions,
        analyses_by_doc=csv_analyses_by_doc,
        merchant_id=merchant_id,
    )
    try:
        score_result = score_deal(
            score_input,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    latest_doc, latest_analysis = items[0]
    latest_transactions = docs.list_transactions(latest_doc.id)
    mca_stack = aggregate_mca_stack(
        transactions=latest_transactions,
        monthly_revenue=latest_analysis.monthly_revenue,
        period_days=latest_analysis.statement_days,
    )
    offer = compute_offer(
        true_revenue_monthly=latest_analysis.monthly_revenue,
        holdback_capacity_monthly=(latest_analysis.monthly_revenue * Decimal("0.25")),
        mca_stack=mca_stack,
    )

    cards = _build_match_cards(
        merchant=merchant,
        score_input=score_input,
        score_result=score_result,
        funder_repo=funder_repo,
        merchants_repo=merchants,
        docs=docs,
        funder_note_subs=funder_note_subs,
        snapshot=snapshot,
        offer=offer,
        bank_warning=_fintech_bank_warning_from_flags(list(latest_doc.all_flags)),
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["# Deal", merchant.business_name])
    writer.writerow(["# Merchant ID", str(merchant.id)])
    writer.writerow(["# State", merchant.state or ""])
    writer.writerow(["# Score", score_result.score])
    writer.writerow(["# Tier", score_result.tier])
    writer.writerow(["# Recommendation", score_result.recommendation])
    writer.writerow(["# Monthly revenue", str(latest_analysis.monthly_revenue)])
    if offer is not None:
        writer.writerow(["# Suggested advance (recommended)", str(offer.recommended_amount)])
        writer.writerow(["# Suggested advance (max)", str(offer.max_amount)])
    else:
        writer.writerow(["# Suggested advance (recommended)", ""])
        writer.writerow(["# Suggested advance (max)", ""])
    writer.writerow([])
    writer.writerow(
        [
            "funder_name",
            "funder_id",
            "match_score",
            "qualifies",
            "color",
            "hard_fails",
            "soft_concerns",
            "estimated_advance",
            "estimated_factor",
            "estimated_holdback_pct",
            "estimated_apr",
        ]
    )
    for c in cards:
        et = c.get("estimated_terms")
        writer.writerow(
            [
                c["funder_name"],
                c["funder_id"],
                c["match_score"],
                "yes" if c["color"] != "red" else "no",
                c["color"],
                " | ".join(c["hard_reasons"]),
                " | ".join(c["soft_concerns"]),
                str(et.estimated_advance) if et and et.estimated_advance is not None else "",
                str(et.estimated_factor) if et and et.estimated_factor is not None else "",
                str(et.estimated_holdback_pct)
                if et and et.estimated_holdback_pct is not None
                else "",
                str(et.estimated_apr) if et and et.estimated_apr is not None else "",
            ]
        )

    filename = f"matched_funders_{slugify(merchant.business_name)}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "cache-control": "private, no-store",
        },
    )


# ---------------------------------------------------------------------------
# Submit to funders (CSV / ZIP download + audit row + submissions rows).
# ---------------------------------------------------------------------------


@router.post("/merchants/{merchant_id}/submit", response_model=None)
async def merchant_submit_to_funders(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    submissions_repo: Annotated[SubmissionRepository, Depends(get_submission_repository)],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

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
    # U33 — Track A/B feed; same shape as the matched-funders panel.
    _submit_documents = [d for d, _ in items]
    _submit_analyses_by_doc = {d.id: a for d, a in items}
    track_a_verdict, track_b_band = compute_score_deal_track_inputs(
        documents=_submit_documents,
        list_transactions=docs.list_transactions,
        analyses_by_doc=_submit_analyses_by_doc,
        merchant_id=merchant_id,
    )
    try:
        score_result = score_deal(
            score_input,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    # mp Phase 2 §12 — immutable decisions row BEFORE the operation
    # returns. Anchor doc is items[0][0] (most-recent analyzed doc,
    # matches the submissions-row anchor below). Snapshot write
    # failures raise 503 here so the operator can retry rather than
    # silently shipping a submission with no audit trail.
    _record_decision_for_score(
        document_id=items[0][0].id,
        deal=score_input,
        result=score_result,
        state_matrix=request.app.state.state_matrix,
        snapshot=snapshot,
        audit=audit,
        decided_by="submit_to_funders",
        actor_email=actor_email,
    )

    # 2026-06-24 offer-sizing wire-through: the per-funder submission
    # CSVs include ``estimated_advance`` which funders read directly.
    # Seed it from the same OfferRecommendation the dossier surfaces
    # so the operator's CSV-submitted advance matches what they see
    # on screen (instead of the legacy crude tier x revenue seed).
    _submit_latest_doc, _submit_latest_analysis = items[0]
    _submit_latest_txns = docs.list_transactions(_submit_latest_doc.id)
    _submit_mca_stack = aggregate_mca_stack(
        transactions=_submit_latest_txns,
        monthly_revenue=_submit_latest_analysis.monthly_revenue,
        period_days=_submit_latest_analysis.statement_days,
    )
    offer = compute_offer(
        true_revenue_monthly=_submit_latest_analysis.monthly_revenue,
        holdback_capacity_monthly=(_submit_latest_analysis.monthly_revenue * Decimal("0.25")),
        mca_stack=_submit_mca_stack,
    )

    requested_set = set(requested_ids)
    matched: list[FunderMatch] = []
    for f in funder_repo.list_active():
        if f.id not in requested_set:
            continue
        m = match_funder(f, score_input, score_result, merchant=merchant, offer=offer)
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
            "dossier_pdf_sha256": (_sha256_hex(dossier_pdf) if dossier_pdf is not None else None),
            "dossier_pdf_filename": dossier_filename,
        },
    )

    # U20: persist one durable submissions row per matched funder. The
    # most-recent analyzed document is the "anchor" doc_id for the
    # natural key ``(merchant_id, document_id, funder_id)``. Proposed
    # terms snapshot the score-result envelope (suggested_max_advance,
    # recommended_factor_rate, recommended_holdback_pct) or fall back
    # to the operator-requested values when the scorer left them at
    # zero (a no-recommendation result).
    anchor_doc = items[0][0]
    proposed_amount = (
        score_result.suggested_max_advance
        if score_result.suggested_max_advance > 0
        else score_input.requested_amount
    )
    # ``recommended_factor_rate_from`` returns None when the scorer
    # didn't produce a meaningful factor (hard decline path, sub-1.0
    # floor). Same "no recommendation" semantics the Close
    # Opportunity sync uses for the Recommended Factor Rate field.
    proposed_factor = _recommended_factor_rate_from(score_result) or score_input.requested_factor
    proposed_holdback = (
        score_result.recommended_holdback_pct
        if score_result.recommended_holdback_pct > 0
        else Decimal("0.1200")
    )
    submitter = actor_email or "dashboard"
    for sub_file in files:
        try:
            record_submission(
                submissions_repo,
                audit,
                merchant_id=merchant.id,
                document_id=anchor_doc.id,
                funder_id=UUID(sub_file.funder_id),
                submitted_by=submitter,
                csv_bytes=sub_file.csv_bytes,
                csv_filename=sub_file.filename,
                proposed_amount=proposed_amount,
                proposed_factor=proposed_factor,
                proposed_holdback=proposed_holdback,
                actor_email=actor_email,
            )
        except SubmissionConflictError:
            # Re-submission to the same funder for the same doc — the
            # durable row already exists. Audit row above captured the
            # event; leave the existing submission untouched (Phase 7C
            # adds an explicit re-submit UPDATE path).
            from aegis.logger import get_logger

            get_logger(__name__).info(
                "submission.duplicate merchant_id=%s funder_id=%s document_id=%s",
                merchant.id,
                sub_file.funder_id,
                anchor_doc.id,
            )
        except SubmissionWriteError as exc:
            # Durable persistence failure → 503. The operator can retry;
            # the submit_to_funders audit row above is the cross-reference.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"submission_persistence_unavailable: {exc}",
            ) from exc

    # Update tracking fields (in-memory implementations only — Supabase
    # path round-trips lose these; durable record is the audit row above).
    try:
        merchants.upsert(
            merchant.model_copy(
                update={
                    "submitted_to_funder_ids": [UUID(sub.funder_id) for sub in files],
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
        headers={"content-disposition": f'attachment; filename="{download_filename}"'},
    )


# ---------------------------------------------------------------------------
# Submit-to-funder note — formats the AEGIS dossier into a plain-text
# summary and posts it to the Close Lead's activity feed.
# ---------------------------------------------------------------------------


def _integrity_verdict_word(verdict: IntegrityVerdict | None) -> str | None:
    """Project a Track A verdict onto the funder-note's one-word slot.

    ``clean`` → ``"clean"``, ``review`` → ``"flagged for review"``,
    ``fail`` → ``"flagged"``, ``None`` → ``None`` (note falls through
    to the formatter's default).
    """
    if verdict is None:
        return None
    if verdict.verdict == "clean":
        return "clean"
    if verdict.verdict == "review":
        return "flagged for review"
    if verdict.verdict == "fail":
        return "flagged"
    return None


@router.post("/merchants/{merchant_id}/submit-to-funder", response_model=None)
async def merchant_submit_to_funder(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> HTMLResponse:
    """Post the AEGIS funder-submission note to the Close Lead activity feed.

    Pipeline:
      1. Load merchant; 404 if missing or not finalized.
      2. Refuse with 400 when ``close_lead_id`` is not set — the dossier
         button is gated on the same condition, but a stale tab could
         POST against a just-unlinked merchant.
      3. Build the score + offer + stack + balance-health rollups from
         the most-recent analyzed bundle, exactly like the matched-
         funders panel does.
      4. Match every active funder against the score result; pass the
         ranked list to ``format_funder_note``.
      5. POST the note via ``CloseClient.post_note``. On 4xx/5xx after
         retries, the Close error propagates as a 502 — the operator
         sees the cause and can retry.
      6. Write ``deal.funder_note_posted`` audit on success, then
         return the "Submitted ✓" swap HTML (HTMX outerHTML).
    """
    return await _perform_submit_to_funder(
        request=request,
        merchant_id=merchant_id,
        target_funder_id=None,
        merchants=merchants,
        docs=docs,
        funder_repo=funder_repo,
        ofac=ofac,
        audit=audit,
        close_client=close_client,
        funder_note_subs=funder_note_subs,
        snapshot=snapshot,
        actor_email=actor_email,
    )


@router.post("/merchants/{merchant_id}/submit-to-funder/{funder_id}", response_model=None)
async def merchant_submit_to_specific_funder(
    request: Request,
    merchant_id: UUID,
    funder_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> HTMLResponse:
    """Phase 7B per-funder submit (2026-06-19).

    Same pipeline as ``merchant_submit_to_funder`` but narrows the
    matched-funder list to the single funder identified in the path.
    Powers the inline § 4 dossier panel's per-funder Submit buttons —
    one button per matched funder, each posting only against that
    specific funder so the Close Note + durable submission row land
    scoped to the operator's actual click. Returns the same HTMX
    swap HTML the global endpoint returns.
    """
    return await _perform_submit_to_funder(
        request=request,
        merchant_id=merchant_id,
        target_funder_id=funder_id,
        merchants=merchants,
        docs=docs,
        funder_repo=funder_repo,
        ofac=ofac,
        audit=audit,
        close_client=close_client,
        funder_note_subs=funder_note_subs,
        snapshot=snapshot,
        actor_email=actor_email,
    )


async def _perform_submit_to_funder(
    *,
    request: Request,
    merchant_id: UUID,
    target_funder_id: UUID | None,
    merchants: MerchantRepository,
    docs: DocumentRepository,
    funder_repo: FunderRepository,
    ofac: OFACClient | None,
    audit: AuditLog,
    close_client: CloseClient,
    funder_note_subs: FunderNoteSubmissionRepository,
    snapshot: DecisionSnapshot,
    actor_email: str | None,
) -> HTMLResponse:
    """Shared implementation for the global + per-funder Submit-to-Funder
    routes. ``target_funder_id`` narrows the matched list to one funder
    when set; otherwise the existing top-ranked-three behavior applies."""
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not merchant.is_finalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("merchant is not finalized — name the merchant before submitting to funder"),
        )

    if not merchant.close_lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"merchant {merchant_id} has no close_lead_id; "
                "submit-to-funder requires a linked Close Lead"
            ),
        )

    items = _collect_analyzed_for_merchant(docs, merchant_id)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no analyzed document — upload + parse first",
        )

    score_input = _score_input_multi_month(merchant, items)
    submit_documents = [d for d, _ in items]
    submit_analyses_by_doc = {d.id: a for d, a in items}
    track_a_verdict, track_b_band = compute_score_deal_track_inputs(
        documents=submit_documents,
        list_transactions=docs.list_transactions,
        analyses_by_doc=submit_analyses_by_doc,
        merchant_id=merchant_id,
        industry_tier=industry_risk_tier(merchant.industry_choice),
    )
    try:
        score_result = score_deal(
            score_input,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    # mp Phase 2 §12 — immutable decisions row BEFORE the operation
    # returns. Anchor on the same most-recent analyzed document the
    # downstream funder-note submission row will anchor on; that's the
    # natural FK target for the snapshot.
    _record_decision_for_score(
        document_id=items[0][0].id,
        deal=score_input,
        result=score_result,
        state_matrix=request.app.state.state_matrix,
        snapshot=snapshot,
        audit=audit,
        decided_by="submit_to_funder",
        actor_email=actor_email,
    )

    latest_doc, latest_analysis = items[0]
    latest_transactions = docs.list_transactions(latest_doc.id)
    mca_stack = aggregate_mca_stack(
        transactions=latest_transactions,
        monthly_revenue=latest_analysis.monthly_revenue,
        period_days=latest_analysis.statement_days,
    )
    balance_health = compute_balance_health(
        transactions=latest_transactions,
        period_days=latest_analysis.statement_days,
    )
    offer = compute_offer(
        true_revenue_monthly=latest_analysis.monthly_revenue,
        holdback_capacity_monthly=(latest_analysis.monthly_revenue * Decimal("0.25")),
        mca_stack=mca_stack,
    )

    matched: list[FunderMatch] = []
    for f in funder_repo.list_active():
        m = match_funder(f, score_input, score_result, merchant=merchant, offer=offer)
        if m is None:
            continue
        matched.append(m)
    matched.sort(key=lambda fm: fm.match_score, reverse=True)

    # Per-funder narrow when the dossier's inline § 4 panel posted a
    # ``funder_id``. The targeted funder MUST exist in ``matched`` —
    # if it doesn't, the dossier UI was stale (e.g., the funder was
    # deactivated between the dossier render and the click) and we
    # refuse with 400 rather than silently posting against the top
    # global match. Empty filtered list also 400s so the "no matches"
    # branch below only fires for the legacy global-submit branch.
    if target_funder_id is not None:
        narrowed = [m for m in matched if m.funder_id == target_funder_id]
        if not narrowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"funder {target_funder_id} is not a current match for this "
                    "merchant — reload the dossier to refresh the panel"
                ),
            )
        matched = narrowed

    # Stipulations gate (Sprint 6 Track A — supersedes the legacy
    # document-completeness check; the legacy adapter still exists for
    # backwards compatibility but the gate now consumes the full
    # ``StipsResult`` bucket). Before posting the Close Note, evaluate
    # the top matched funder's ``conditional_requirements`` against the
    # merchant's on-file flags and refuse with 400 when:
    #   * any STRUCTURED missing stip (voided check / driver's license /
    #     N-months statements) is unmet, OR
    #   * any UNKNOWN missing stip is hard ("must provide ...",
    #     "required ..."). Soft-worded unknowns ("nice-to-have ACH proof")
    #     do NOT gate — operator owns judgement on those via dossier
    #     review. Documented here so the next session understands the
    #     policy: known unknowns we can verify (structured kinds) always
    #     gate; unknown unknowns gate only when the funder used hard
    #     language. Empty ``matched`` short-circuits (the no-match
    #     branch downstream records ``submission_skipped_no_matches``).
    if matched:
        top_funder = funder_repo.get(matched[0].funder_id)
        top_stips_result = evaluate_stips(top_funder, merchant)
        gating_missing = [item for item in top_stips_result.missing if item.is_hard]
        if gating_missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "stipulations_unmet",
                    "top_funder_id": str(top_funder.id),
                    "top_funder_name": top_funder.name,
                    "missing": [item.model_dump() for item in gating_missing],
                },
            )

    note_text = format_funder_note(
        merchant=merchant,
        score_result=score_result,
        offer=offer,
        mca_stack=mca_stack,
        balance_health=balance_health,
        industry_tier=industry_risk_tier(merchant.industry_choice),
        matched_funders=matched,
        months_of_statements=len(items),
        true_revenue_monthly=latest_analysis.monthly_revenue,
        integrity_verdict=_integrity_verdict_word(track_a_verdict),
        num_nsf=score_input.num_nsf,
        days_negative=score_input.days_negative,
    )

    # Prepend the Bedrock-generated funder-facing narrative when
    # available. Empty string when Bedrock was unavailable — note
    # falls back to the structured-only format. The narrative is the
    # same one the dossier renders in the Funder summary card, so
    # operators see what gets posted before clicking Submit.
    from aegis.scoring_v2.deal_summary import (
        CloseContext as _CloseCtx,
    )
    from aegis.scoring_v2.deal_summary import (
        generate_funder_narrative as _gen_narrative,
    )

    narrative = _gen_narrative(
        merchant=merchant,
        score_result=score_result,
        mca_stack=mca_stack,
        balance_health=balance_health,
        offer=offer,
        close_context=_CloseCtx(
            lead_description=merchant.close_lead_description,
            notes_summary=merchant.close_notes_summary,
            call_transcripts=merchant.close_call_transcripts,
        ),
    )
    if narrative:
        note_text = f"{narrative}\n\n{note_text}"

    try:
        close_response = close_client.post_note(
            merchant.close_lead_id,
            note_text,
        )
    except CloseError as exc:
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="deal.funder_note_post_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": merchant.close_lead_id,
                "status_code": exc.status_code,
                "error": str(exc)[:200],
                "note_length": len(note_text),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"close note POST failed: {exc}",
        ) from exc

    # The route ranks all matched funders and posts ONE Close Note
    # covering the top three. The funder_note_submissions table is
    # one-row-per-click, framed against the top matched funder. When
    # there are no matched funders (rare — e.g. fresh merchant with a
    # very narrow industry tier), the Note has still been posted so we
    # don't unwind the Close write; we just skip the durable row and
    # record ``submission_skipped_no_matches`` on the audit row so a
    # later operator question can still surface the click.
    submission_row_id: UUID | None = None
    if matched:
        top_funder_id = matched[0].funder_id
        submission_row = funder_note_subs.create(
            merchant_id=merchant.id,
            funder_id=top_funder_id,
            funder_note=note_text,
            submitted_by=actor_email or "dashboard",
        )
        submission_row_id = submission_row.id

    audit_details: dict[str, Any] = {
        "close_lead_id": merchant.close_lead_id,
        "close_activity_id": close_response.get("id"),
        "note_length": len(note_text),
        "score": score_result.score,
        "tier": score_result.tier,
        "matched_funder_count": len(matched),
    }
    if target_funder_id is not None:
        audit_details["target_funder_id"] = str(target_funder_id)
    if submission_row_id is not None:
        audit_details["funder_note_submission_id"] = str(submission_row_id)
    else:
        audit_details["submission_skipped_no_matches"] = True

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="deal.funder_note_posted",
        subject_type="merchant",
        subject_id=merchant.id,
        details=audit_details,
    )

    return HTMLResponse(
        content=(
            '<span class="btn primary is-disabled" '
            'data-submitted-to-funder="true" aria-disabled="true">'
            "Submitted &check;</span>"
        ),
    )


# ---------------------------------------------------------------------------
# Prepare-renewal — Sprint 7 Track B. The operator clicks "Prepare Renewal"
# in the /ui/renewals pipeline; AEGIS reruns scoring on the latest analyzed
# statements, sizes a fresh offer, picks the top funder match, then posts
# a renewal-flagged note to Close. The original-funding header is pulled
# from the merchant's FIRST approved funder_note_submission (when one
# exists — when none, the route still completes and just omits the header,
# but flags the durable submission row as a renewal on the audit row).
# ---------------------------------------------------------------------------


_RENEWAL_DAYS_PER_MONTH: Final[int] = 30


@router.post("/merchants/{merchant_id}/prepare-renewal", response_model=None)
async def merchant_prepare_renewal(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> HTMLResponse:
    """Re-run scoring + post a renewal-flagged Close Note for the merchant.

    Sequence:
      1. Load merchant; 404 when missing.
      2. Look up the FIRST approved ``funder_note_submission`` for this
         merchant (operator's renewal anchor — first approval is the
         original funding decision). When none exists, the route still
         proceeds; the note posts without the RENEWAL header and the
         audit row's ``original_funding_date`` + ``original_amount``
         carry ``None``.
      3. 400 when ``close_lead_id`` is unset — the Close write target
         is missing and there's nothing to do.
      4. Rerun the scoring pipeline on the latest analyzed statements —
         same path as ``merchant_submit_to_funder``: collect items,
         build the multi-month ScoreInput, compute Track A/B inputs,
         call ``score_deal``.
      5. Size a recommended offer via ``compute_offer``.
      6. Match every active funder; pick the top by ``match_score``.
      7. Format the funder note with a ``RenewalContext`` when prior-
         funding data is available; otherwise pass ``None``.
      8. POST the note to Close.
      9. Insert one ``funder_note_submissions`` row framed against the
         top matched funder so the renewal lands in submission history.
     10. Write ``deal.renewal_prepared`` audit row with all the prior-
         funding + new-offer + top-funder + close-note details.
     11. Return an HTMX outerHTML swap targeting
         ``#renewal-row-{merchant_id}`` carrying a "Renewal package
         ready" affirmation with the UTC timestamp.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not merchant.close_lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"merchant {merchant_id} has no close_lead_id; "
                "prepare-renewal requires a linked Close Lead"
            ),
        )

    # Find the FIRST approved submission — that's the original funding
    # decision. ``list_for_merchant`` returns newest-first; we iterate
    # the full list (capped at the default limit, fine for any real
    # merchant) and pick the oldest approved row. No approval row =
    # graceful skip per spec.
    history = funder_note_subs.list_for_merchant(merchant.id)
    approved_rows = [r for r in history if r.status == "approved"]
    original_submission = (
        min(approved_rows, key=lambda r: r.submitted_at) if approved_rows else None
    )
    original_funding_date: date | None = (
        original_submission.submitted_at.date() if original_submission is not None else None
    )
    original_amount: Decimal | None = (
        original_submission.offer_amount if original_submission is not None else None
    )

    items = _collect_analyzed_for_merchant(docs, merchant_id)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="merchant has no analyzed document — upload + parse first",
        )

    score_input = _score_input_multi_month(merchant, items)
    submit_documents = [d for d, _ in items]
    submit_analyses_by_doc = {d.id: a for d, a in items}
    track_a_verdict, track_b_band = compute_score_deal_track_inputs(
        documents=submit_documents,
        list_transactions=docs.list_transactions,
        analyses_by_doc=submit_analyses_by_doc,
        merchant_id=merchant_id,
        industry_tier=industry_risk_tier(merchant.industry_choice),
    )
    try:
        score_result = score_deal(
            score_input,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except OFACStaleError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ofac_unavailable: {exc}",
        ) from exc

    latest_doc, latest_analysis = items[0]
    latest_transactions = docs.list_transactions(latest_doc.id)
    mca_stack = aggregate_mca_stack(
        transactions=latest_transactions,
        monthly_revenue=latest_analysis.monthly_revenue,
        period_days=latest_analysis.statement_days,
    )
    balance_health = compute_balance_health(
        transactions=latest_transactions,
        period_days=latest_analysis.statement_days,
    )
    offer = compute_offer(
        true_revenue_monthly=latest_analysis.monthly_revenue,
        holdback_capacity_monthly=(latest_analysis.monthly_revenue * Decimal("0.25")),
        mca_stack=mca_stack,
    )

    matched: list[FunderMatch] = []
    for f in funder_repo.list_active():
        m = match_funder(f, score_input, score_result, merchant=merchant, offer=offer)
        if m is None:
            continue
        matched.append(m)
    matched.sort(key=lambda fm: fm.match_score, reverse=True)

    # mp Phase 2 §12 — immutable decisions row BEFORE the operation
    # returns. Renewal scoring writes a fresh decisions snapshot so the
    # audit trail captures the new score / tier / recommendation that
    # drove the renewal note posted below, not just the original-
    # funding-cycle decision row backfilled at first funding.
    _record_decision_for_score(
        document_id=items[0][0].id,
        deal=score_input,
        result=score_result,
        state_matrix=request.app.state.state_matrix,
        snapshot=snapshot,
        audit=audit,
        decided_by="prepare_renewal",
        actor_email=actor_email,
    )

    # Build the renewal-context header only when both halves are present.
    # Original_amount can be None even with a prior approved row if the
    # operator forgot to capture the funded amount on the response form;
    # in that case the header would be ``$None`` which is worse than no
    # header at all. Same gate for original_funding_date.
    renewal_context: RenewalContext | None = None
    months_since_funding: int | None = None
    if original_funding_date is not None and original_amount is not None:
        months_since_funding = (
            datetime.now(UTC).date() - original_funding_date
        ).days // _RENEWAL_DAYS_PER_MONTH
        renewal_context = RenewalContext(
            original_funding_date=original_funding_date,
            original_amount=original_amount,
            months_since_funding=months_since_funding,
        )

    note_text = format_funder_note(
        merchant=merchant,
        score_result=score_result,
        offer=offer,
        mca_stack=mca_stack,
        balance_health=balance_health,
        industry_tier=industry_risk_tier(merchant.industry_choice),
        matched_funders=matched,
        months_of_statements=len(items),
        true_revenue_monthly=latest_analysis.monthly_revenue,
        integrity_verdict=_integrity_verdict_word(track_a_verdict),
        num_nsf=score_input.num_nsf,
        days_negative=score_input.days_negative,
        renewal_context=renewal_context,
    )

    try:
        close_response = close_client.post_note(
            merchant.close_lead_id,
            note_text,
        )
    except CloseError as exc:
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="deal.renewal_note_post_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": merchant.close_lead_id,
                "status_code": exc.status_code,
                "error": str(exc)[:200],
                "note_length": len(note_text),
                "is_renewal": True,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"close note POST failed: {exc}",
        ) from exc

    # Insert one durable funder_note_submissions row so the renewal
    # appears in submission history. Framed against the top matched
    # funder (same convention as ``merchant_submit_to_funder``). When
    # there are zero matched funders we still want the renewal to be
    # auditable — the audit row below carries the flag, but no
    # submissions row is created (there's no funder_id to anchor it).
    submission_row_id: UUID | None = None
    top_funder_id: UUID | None = None
    if matched:
        top_funder_id = matched[0].funder_id
        submission_row = funder_note_subs.create(
            merchant_id=merchant.id,
            funder_id=top_funder_id,
            funder_note=note_text,
            submitted_by=actor_email or "dashboard",
        )
        submission_row_id = submission_row.id

    # Operator task in Close prompting the next renewal step. Best-effort:
    # a Close API error on the task POST audits but does NOT roll back the
    # post_note or submission row — the operator-visible note + AEGIS-side
    # submission row are the load-bearing parts of the renewal workflow.
    renewal_task_text = (
        f"Request updated bank statements from {merchant.business_name}"
        + (
            f" — renewal eligible {merchant.maturity_date.isoformat()}."
            if merchant.maturity_date is not None
            else " — renewal package prepared."
        )
        + " Re-score before submitting for renewal."
    )
    try:
        close_client.create_task(
            lead_id=merchant.close_lead_id,
            text=renewal_task_text,
            due_date=date.today(),
        )
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="close.task.renewal_prepared",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": merchant.close_lead_id,
                "task_text": renewal_task_text,
                "due_date": date.today().isoformat(),
            },
        )
    except CloseError as exc:
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="close.task.renewal_prepared_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": merchant.close_lead_id,
                "status_code": exc.status_code,
                "error": str(exc)[:200],
            },
        )

    close_note_id = close_response.get("id")
    audit_details: dict[str, Any] = {
        "merchant_id": str(merchant.id),
        "close_lead_id": merchant.close_lead_id,
        "close_note_id": close_note_id,
        "is_renewal": True,
        "original_funding_date": (
            original_funding_date.isoformat() if original_funding_date is not None else None
        ),
        "original_amount": (str(original_amount) if original_amount is not None else None),
        "months_since_funding": months_since_funding,
        "new_recommended_amount": (str(offer.recommended_amount) if offer is not None else None),
        "new_factor": (
            str(score_result.recommended_factor_rate)
            if score_result.recommended_factor_rate > 0
            else None
        ),
        "top_funder_id": str(top_funder_id) if top_funder_id is not None else None,
        "score": score_result.score,
        "tier": score_result.tier,
        "matched_funder_count": len(matched),
        "note_length": len(note_text),
    }
    if submission_row_id is not None:
        audit_details["funder_note_submission_id"] = str(submission_row_id)
    else:
        audit_details["submission_skipped_no_matches"] = True

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="deal.renewal_prepared",
        subject_type="merchant",
        subject_id=merchant.id,
        details=audit_details,
    )

    now_stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return HTMLResponse(
        content=(
            f'<tr id="renewal-row-{merchant.id}" '
            f'data-test-id="renewal-pipeline-row" '
            f'data-merchant-id="{merchant.id}" '
            f'data-renewal-prepared="true">'
            f'<td colspan="6" class="merchant">'
            f'<span class="chip pos">&check; Renewal package ready</span> '
            f'<span class="sub">posted to Close at {now_stamp}</span>'
            f"</td>"
            f"</tr>"
        ),
    )


# ---------------------------------------------------------------------------
# Merchant notes — Feature C operator-notes panel (migration 066).
#
# Replaces the legacy single-text-column append-only ``merchants.notes``
# (migration 058) with a normalized one-row-per-note table. Each save is a
# distinct row with stable id + actor + created_at; the dossier renders
# them newest-first as timestamped cards.
# ---------------------------------------------------------------------------


# Application-layer cap on a single note body. Mirrors the DB CHECK
# constraint on ``merchant_notes.body`` (migration 066) so a request that
# would fail the DB check is rejected at the route boundary with a
# clean 400 instead of bubbling up a SQL error.
MERCHANT_NOTE_BODY_MAX_CHARS: Final[int] = MERCHANT_NOTE_MAX_CHARS


@router.post("/merchants/{merchant_id}/notes", response_model=None)
async def merchant_save_note(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    body: Annotated[str, Form()],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> RedirectResponse:
    """Persist one operator note row and redirect to the dossier.

    Validation (in order — first failure short-circuits with a 4xx):

      * Merchant must exist (``MerchantRepository.get`` → 404 on miss).
      * ``body.strip()`` must be non-empty (400) — an empty textarea
        submission is operator error, not a no-op.
      * ``len(body.strip())`` must be ≤
        ``MERCHANT_NOTE_BODY_MAX_CHARS`` (400) — mirrors the DB CHECK.

    Audit (per CLAUDE.md auditability rule): one ``merchant.note.added``
    row per successful save. ``details`` carries the length ONLY, never
    the body bytes — note bodies are operator-curated free text that may
    quote merchant names / broker context.

    Returns 303 See Other to ``/ui/merchants/{merchant_id}`` so a browser
    POST→reload cycle lands on the dossier with the new card in place.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    stripped = body.strip()
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="note body is empty",
        )
    if len(stripped) > MERCHANT_NOTE_BODY_MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"note body exceeds {MERCHANT_NOTE_BODY_MAX_CHARS}-char limit",
        )

    actor = actor_email or "dashboard"
    merchants.add_note(merchant_id=merchant.id, body=stripped, actor=actor)

    audit.record(
        actor="operator",
        actor_email=actor_email,
        action="merchant.note.added",
        subject_type="merchant",
        subject_id=merchant.id,
        details={"length": len(stripped)},
    )

    # 303 (See Other) is the canonical POST→GET pattern. RedirectResponse
    # default is 307 (Temporary Redirect) which preserves the verb — wrong
    # for a form submission. Explicit 303 makes the GET / dossier-render
    # path explicit.
    return RedirectResponse(
        url=f"/ui/merchants/{merchant.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Migration 065 — operator-initiated soft-delete from the dossier.
# ---------------------------------------------------------------------------


@router.post("/merchants/{merchant_id}/delete", response_model=None)
async def merchant_soft_delete(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> RedirectResponse:
    """Soft-delete a merchant from the dossier surface.

    POST (not DELETE) so the dossier header can submit a plain HTML form
    without JS. Confirms the merchant exists and is not already deleted,
    flips ``merchants.deleted_at`` to now, writes one
    ``merchant.deleted`` audit row, and redirects to the merchants list.

    The repository's read methods filter ``deleted_at IS NOT NULL`` so
    no caller (dashboard, portfolio, renewals, match panel) needs to
    change — the merchant simply disappears from every list and a
    direct hit to ``/ui/merchants/{id}`` returns 404. All descendant
    documents / transactions / analyses / decisions / audit rows are
    preserved per CLAUDE.md auditability.

    Audit-write failure propagates per CLAUDE.md rule. The repo write
    runs first so a soft-delete that the operator sees succeed is
    always reflected in the audit log; an audit-write failure after a
    successful repo flip surfaces a 500 but does NOT roll back the
    repo write (the merchant stays soft-deleted) — same posture as the
    notes route above.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    now = datetime.now(UTC)
    try:
        merchants.soft_delete(merchant_id, deleted_at=now)
    except MerchantNotFoundError as exc:
        # Concurrent double-submit: the repo guard returns NotFound for
        # an already-deleted row even though ``get`` succeeded a moment
        # ago. Surface as 404 so the operator sees a normal not-found
        # page rather than a 500.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    audit.record(
        actor="operator",
        actor_email=actor_email,
        action="merchant.deleted",
        subject_type="merchant",
        subject_id=merchant.id,
        details={"business_name": merchant.business_name},
    )

    return RedirectResponse("/ui/merchants", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Feature D — merchant context fields (migration 064)
# ---------------------------------------------------------------------------


_DEAL_CONTEXT_MAX_CHARS: Final[int] = 8000


@router.post("/merchants/{merchant_id}/deal-context", response_model=None)
async def merchant_set_deal_context(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    deal_context: Annotated[str, Form()] = "",
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> RedirectResponse:
    """Persist the operator-written ``deal_context`` for a merchant.

    Feature D — surfaces the textarea on the dossier Context panel.
    Trims surrounding whitespace; an empty submission clears the field
    (NULL on the DB) — distinct from the operator-notes flow which is
    append-only and treats empty submissions as no-ops, because
    ``deal_context`` is a single replaceable note rather than a
    timestamped log.

    Audit (per CLAUDE.md auditability rule): one
    ``merchant.deal_context.updated`` row per successful save.
    ``details`` carries the new length only — never the body bytes,
    per the migration-064 PII posture (operator may quote merchant
    identity / processor names in the textarea).
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    trimmed = deal_context.strip()
    if len(trimmed) > _DEAL_CONTEXT_MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"deal_context exceeds {_DEAL_CONTEXT_MAX_CHARS}-char limit",
        )

    normalized = trimmed or None
    merchants.set_deal_context(merchant.id, normalized)

    audit.record(
        actor="operator",
        actor_email=actor_email,
        action="merchant.deal_context.updated",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "length": len(trimmed),
            "cleared": normalized is None,
        },
    )

    return RedirectResponse(
        url=f"/ui/merchants/{merchant.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/merchants/{merchant_id}/close-context/refresh", response_model=None)
async def merchant_refresh_close_context(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    close_client: Annotated[CloseClient, Depends(get_close_client)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> RedirectResponse:
    """Force-refresh the merchant's Close-derived context columns.

    Feature D — surfaces the "Refresh Close fields" button on the
    dossier Context panel. Synchronous: the operator gets the new
    values on the post-redirect dossier render.

    Requires a linked ``close_lead_id`` (404 otherwise — the panel hides
    the button when no lead is linked, but this guards against a stale
    tab submitting after the operator unlinked the lead).

    Close API failures surface as 503 — distinct from the webhook
    posture which swallows Close failures to keep the webhook 200-OK
    for Close. Here the operator clicked the button explicitly and
    deserves to see the failure rather than being silently misled into
    thinking the refresh succeeded.

    Audit (success path): the ``merchant.close_context.refreshed`` row
    is written inside ``refresh_close_context_for_merchant`` with the
    pulled counts. This route adds a ``trigger`` field to the actor
    so the operator can distinguish manual refreshes from
    webhook-driven ones in the audit log surface.
    """
    # Local import to avoid the circular import the
    # ``aegis.merchants.close_context`` module would otherwise trigger
    # (close_context already imports from aegis.merchants, and
    # importing close_context here pulls aegis.merchants.repository
    # through the same package init).
    from aegis.merchants.close_context import refresh_close_context_for_merchant

    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not merchant.close_lead_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="merchant has no Close lead linked",
        )

    try:
        refresh_close_context_for_merchant(
            merchant.id,
            merchant.close_lead_id,
            close_client=close_client,
            merchants_repo=merchants,
            audit=audit,
        )
    except CloseError as exc:
        # Operator-initiated — surface the Close failure so they can
        # retry / escalate rather than swallowing it like the webhook
        # path does. Audit row for the failure mirrors the webhook
        # best-effort wrapper so the audit log has a uniform shape.
        audit.record(
            actor="operator",
            actor_email=actor_email,
            action="merchant.close_context.refresh_failed",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": merchant.close_lead_id,
                "status_code": exc.status_code,
                "error": str(exc)[:200],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"close_context_refresh_failed: {exc}",
        ) from exc

    return RedirectResponse(
        url=f"/ui/merchants/{merchant.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/merchants/{merchant_id}/refresh-ucc", response_model=None)
async def merchant_refresh_ucc(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> RedirectResponse:
    """Force-run the UCC + previous-default check for this merchant.

    Same posture as the web-presence refresh: always runs, Bedrock
    failure persists empty result + audit row so the operator sees
    the click did something.
    """
    from aegis.business_intel.refresh import refresh_ucc_for_merchant

    try:
        refresh_ucc_for_merchant(
            merchant_id,
            merchants_repo=merchants,
            audit=audit,
        )
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return RedirectResponse(
        url=f"/ui/merchants/{merchant_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/merchants/{merchant_id}/refresh-web-presence", response_model=None)
async def merchant_refresh_web_presence(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> RedirectResponse:
    """Force-run the reputation scan for this merchant.

    Always runs (no idempotency check on ``web_presence_scanned_at``).
    Failure modes are absorbed inside ``scan_web_presence`` — Bedrock
    unavailable / web_search tool unsupported / malformed response all
    collapse to an empty result, which the orchestrator still persists
    so the dossier shows the empty state instead of "never scanned".
    The audit row carries ``bedrock_succeeded`` so the operator can
    distinguish "scanned and found nothing" from "scan failed".
    """
    from aegis.web_presence.refresh import refresh_web_presence_for_merchant

    try:
        refresh_web_presence_for_merchant(
            merchant_id,
            merchants_repo=merchants,
            audit=audit,
        )
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return RedirectResponse(
        url=f"/ui/merchants/{merchant_id}",
        status_code=status.HTTP_303_SEE_OTHER,
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        amount = _decimal_or_none(offered_amount)
        factor = _decimal_or_none(offered_factor)
        term = _int_or_none(offered_term_days)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

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
    return RedirectResponse(url=f"/ui/merchants/{merchant.id}/match", status_code=303)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if merchant.close_lead_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"merchant {merchant_id} has no close_lead_id; rescan requires a linked Close Lead"
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

    return RedirectResponse(url=f"/ui/merchants/{merchant.id}", status_code=303)


# ---------------------------------------------------------------------------
# Detail / dossier / findings helpers.
# ---------------------------------------------------------------------------


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


def _latest_funder_responses(audit: AuditLog, merchant_id: UUID) -> dict[str, dict[str, Any]]:
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


def _maybe_render_dossier_pdf(
    *,
    merchant: MerchantRow,
    docs: DocumentRepository,
    ofac: OFACClient | None,
    funder_repo: FunderRepository | None = None,
    funder_note_subs: FunderNoteSubmissionRepository | None = None,
) -> tuple[bytes | None, str | None]:
    """Render the merchant's PDF dossier (operator review + audit), or fail soft.

    Returns ``(pdf_bytes, filename)`` on success; ``(None, None)`` if the
    Hetzner box / WSL2 native libs are unavailable. The submission flow
    must not fail just because a PDF can't be produced — the CSV ZIP
    download and the audit row are the authoritative record.

    ``funder_repo`` + ``funder_note_subs`` are optional. When supplied the
    PDF gains the top-funder-matches and submission-history sections —
    both web-dossier features. Legacy callers (e.g. the submit-to-funders
    flow which historically built without these repos) keep working with
    the new sections omitted; they still get the verdict + cashflow +
    pattern + state + statements package.
    """
    try:
        import weasyprint

        context = _build_pdf_dossier_context(
            merchant,
            docs,
            ofac,
            funder_repo=funder_repo,
            funder_note_subs=funder_note_subs,
        )
        html = templates.get_template("merchant_detail_dossier_pdf.html.j2").render(context)
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


_SHADOW_FLAG_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^\[(?:SHADOW|WARN)\]\s*")


def _collect_shadow_flags_for_dossier(
    *,
    latest_doc: DocumentRow | None,
    pattern_analysis: PatternAnalysis | None,
    score_result: ScoreResult | None,
) -> list[HumanFlag]:
    """Roll up humanized shadow flags across every source on the deal.

    Sources:
      * ``latest_doc.all_flags`` — the parser writes shadow-mode entries
        as ``[SHADOW] code:detail`` (nsf_secondary R1.8, adb_coverage_thin
        R1.7) and validation warnings as ``[WARN] code:detail``
        (daily_balance_continuity_break R1.4,
        transaction_id_sequence_gap R1.5). We strip both prefixes so
        ``humanize_flag`` reaches the registry.
      * ``pattern_analysis.shadow_patterns`` — R1.1 / R1.3 fuzzy / disguise
        / same-day cluster + M9 structured_deposit_cluster. Stored as
        ``Pattern(severity=0, code=..., detail=...)``; we synthesize
        the ``code:detail`` shape ``humanize_flag`` expects.
      * ``score_result.shadow_flags`` — R3.4 state_enforcement_concern,
        R4.4 seasonality, R4.6 eof_policy_mismatch, H8 tib_ramp_shadow.
        Bare ``code:detail`` strings, no prefix.

    Returns a list of humanized ``HumanFlag`` objects deduped by code,
    ordered source-by-source so the document-level flags come first,
    then pattern-level, then score-level. Empty list when no shadow
    signal fires on the deal — the dossier section guards on truthiness
    and renders nothing.

    Per CLAUDE.md "Decision-boundary changes — shadow-first": every
    item in this list is informational. The dossier renders it inside
    a collapsed ``<details>`` so it never competes with hard-decline
    reasoning.
    """
    seen: set[str] = set()
    out: list[HumanFlag] = []

    def _push(raw: str) -> None:
        if not raw:
            return
        cleaned = _SHADOW_FLAG_PREFIX_RE.sub("", raw, count=1).strip()
        if not cleaned:
            return
        hf = humanize_flag(cleaned)
        if hf.code in seen:
            return
        # Filter to shadow-class entries only — the routine doc flag
        # cache also includes [WARN] math-failure warnings unrelated to
        # the shadow families. Use the category as the gate so the set
        # stays in lockstep with the registry.
        if hf.category != "shadow":
            return
        seen.add(hf.code)
        out.append(hf)

    if latest_doc is not None and latest_doc.all_flags:
        for raw in latest_doc.all_flags:
            _push(raw)

    if pattern_analysis is not None:
        for p in pattern_analysis.shadow_patterns:
            if p.detail:
                _push(f"{p.code}:{p.detail}")
            else:
                _push(p.code)

    if score_result is not None and score_result.shadow_flags:
        for raw in score_result.shadow_flags:
            _push(raw)

    return out


@dataclass(frozen=True)
class MerchantShadowSignalView:
    """Dossier-render shape for one persisted merchant-scope shadow signal.

    Pairs the U18-humanized ``HumanFlag`` (title + detail + description
    via the same registry the per-document section consumes) with the
    persisted timestamp + raw record id from ``merchants_shadow_signals``.
    Lets the template show "Duplicate PDF upload — uploaded 2026-06-08
    14:32" instead of a bare code, and the ``data-signal-id`` attribute
    keeps the audit drill-down clickable.
    """

    record: MerchantShadowSignalRecord
    humanized: HumanFlag


def _humanize_merchant_shadow_signals(
    rows: list[MerchantShadowSignalRecord],
) -> list[MerchantShadowSignalView]:
    """Run each persisted merchant-scope shadow row through the U18
    humanizer so the dossier section uses the same title / detail /
    description registry as the per-document signals above it.

    The persisted ``detail`` string was produced by the U12 detector
    in the ``code:detail`` shape the humanizer expects (e.g.
    ``duplicate_pdf_upload:sha256_match_with_doc=...:uploaded=...``);
    we reassemble it from ``record.signal_code`` + ``record.detail`` so
    the humanizer's per-code formatter fires cleanly. Rows whose
    ``detail`` is ``None`` (defensive — the detector always emits one
    today, but the column is nullable) humanize on the bare code.

    Returns the list in input order so the repository's newest-first
    sort survives to the template.
    """
    out: list[MerchantShadowSignalView] = []
    for row in rows:
        if row.detail:
            raw = f"{row.signal_code}:{row.detail}"
        else:
            raw = row.signal_code
        out.append(
            MerchantShadowSignalView(
                record=row,
                humanized=humanize_flag(raw),
            )
        )
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


# ---------------------------------------------------------------------------
# Original PDF stream — chunk-C of the PDF retention redesign.
#
# 2026-06-15 operator directive: the chunk-B simplification stores AES-GCM
# ciphertext in the Postgres ``pdf_store`` table (migration 060). This
# route decrypts on demand and streams plaintext bytes back to the
# operator. NEVER returns a Supabase signed URL — the security-invariant
# test in tests/test_security_invariants.py greps source for the
# forbidden URL-helper names and fails CI if either appears (see
# docs/PDF_RETENTION_DESIGN.md §9).
#
# Auth: CF Access middleware gates /ui/ routes; no per-route auth here.
# Cross-merchant access: the route enforces ``document.merchant_id ==
# merchant_id`` so an operator with knowledge of one merchant's id +
# another merchant's document id cannot pull the second merchant's PDF
# through the first merchant's path. Mismatch → 404 (not 403) so the
# response surface gives no signal that the document exists at all.
# ---------------------------------------------------------------------------


_PDF_CONTENT_DISPOSITION_HEADER = 'inline; filename="{document_id}.pdf"'


@router.get(
    "/merchants/{merchant_id}/documents/{document_id}/pdf",
    response_model=None,
)
async def merchant_document_pdf(
    merchant_id: UUID,
    document_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    pdf_store: Annotated[PdfStoreRepository, Depends(get_pdf_store_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> Response:
    """Stream the original (plaintext) PDF for a merchant's document.

    Pipeline:
      1. Verify the merchant exists (404 if not).
      2. Load the document; 404 on missing OR on a merchant_id mismatch
         (prevents cross-merchant access).
      3. Fetch + decrypt via ``pdf_store.fetch_plaintext``. SHA-256 is
         verified inside the repository — a mismatch raises
         ``PdfStoreIntegrityError`` which maps to HTTP 500 + a
         ``document.pdf_streamed_integrity_failed`` audit row.
      4. Audit ``document.pdf_streamed`` (char-count + merchant scope —
         NEVER bytes per CLAUDE.md PII rule). Stream the bytes.
    """
    try:
        merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        document = docs.get_document(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if document.merchant_id != merchant_id:
        # Cross-merchant access attempt. 404 (not 403) so the response
        # surface gives no signal the document exists under another
        # merchant — same posture as a missing row. Audit the attempt
        # so an operator misuse pattern surfaces in the log.
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="document.pdf_streamed_denied",
            subject_type="document",
            subject_id=document_id,
            details={
                "reason": "cross_merchant_access",
                "requested_merchant_id": str(merchant_id),
                "owning_merchant_id": (str(document.merchant_id) if document.merchant_id else None),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"document {document_id} not found under merchant {merchant_id}",
        )

    try:
        plaintext = pdf_store.fetch_plaintext(document_id)
    except PdfStoreNotFoundError as exc:
        # Pre-chunk-B documents OR a document whose pdf_store row was
        # never written (worker store-step failure). 404 — the dossier
        # link should only render when the row exists, but a stale tab
        # could still POST.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no pdf_store row for document {document_id}",
        ) from exc
    except (CorruptCiphertextError, PdfStoreIntegrityError) as exc:
        # Integrity failure — auth-tag rejection OR SHA-256 mismatch.
        # 500 with ``original_viewed_integrity_failed`` audit row per
        # CLAUDE.md PDF retention rule.
        audit.record(
            actor="dashboard",
            actor_email=actor_email,
            action="document.pdf_streamed_integrity_failed",
            subject_type="document",
            subject_id=document_id,
            details={
                "merchant_id": str(merchant_id),
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"pdf integrity check failed for document {document_id}",
        ) from exc
    except PdfStoreWriteError as exc:
        # Read-side Supabase failure (rare). Surface as 503; the
        # operator retries.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"pdf store read failed: {exc}",
        ) from exc

    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="document.pdf_streamed",
        subject_type="document",
        subject_id=document_id,
        details={
            "merchant_id": str(merchant_id),
            "byte_size": len(plaintext),
            # SHA-256 is the integrity signal; never log the bytes.
            "sha256_prefix": hashlib.sha256(plaintext).hexdigest()[:16],
        },
    )

    return Response(
        content=plaintext,
        media_type="application/pdf",
        headers={
            "content-disposition": _PDF_CONTENT_DISPOSITION_HEADER.format(document_id=document_id),
            # Defense-in-depth: even though the operator-facing dossier
            # is the only caller, mark the response un-cacheable so a
            # browser back-button doesn't surface bytes without a
            # fresh audit row.
            "cache-control": "private, no-store",
        },
    )


# ---------------------------------------------------------------------------
# Merchant detail (HTML dossier) + PDF dossier + findings.csv
# ---------------------------------------------------------------------------


@router.get("/merchants/{merchant_id}", response_class=HTMLResponse)
async def merchant_detail(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    shadow_signals_repo: Annotated[
        MerchantShadowSignalRepository,
        Depends(get_merchant_shadow_signal_repository),
    ],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
    snapshot: Annotated[DecisionSnapshot, Depends(get_decision_snapshot)],
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
    score_input: ScoreInput | None = None
    stacking = None
    mca_stack = None
    balance_health = None
    offer = None
    revenue_trends = None
    score_window = None
    bundle_summaries: list[dict[str, Any]] = []
    statement_coverage: dict[str, Any] | None = None
    pattern_cards: list[Any] = []
    # Surfaced to the dossier template so the evidence drill-down's
    # preloan_spike baseline panel can look beyond ``card.source_transactions``
    # and pull pre-spike deposits for the comparison reference.
    latest_transactions: list[Any] = []
    soft_signals = (
        parse_soft_signal_flags(list(latest_doc.all_flags)) if latest_doc is not None else None
    )
    # Held for the post-template ``has_concentration_pattern`` check so
    # the suppression doesn't re-walk the AnalysisRow cache.
    pattern_analysis_for_view: Any = None
    if latest_doc is not None and latest_analysis is not None:
        all_items = _collect_analyzed_for_merchant(docs, merchant_id, window=999, bundle=None)
        bundle_options = _bundle_keys_for_merchant(all_items)
        if selected_bundle is not None and selected_bundle not in {k for k, _ in bundle_options}:
            selected_bundle = None
        items = _collect_analyzed_for_merchant(docs, merchant_id, bundle=selected_bundle)
        active_bundle = (
            selected_bundle if selected_bundle is not None else _select_default_bundle(all_items)
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
        mca_stack = aggregate_mca_stack(
            transactions=latest_transactions,
            monthly_revenue=latest_analysis.monthly_revenue,
            period_days=latest_analysis.statement_days,
        )
        balance_health = compute_balance_health(
            transactions=latest_transactions,
            period_days=latest_analysis.statement_days,
        )
        # ``holdback_capacity_monthly`` is operator-confirmed on the Close
        # Opportunity ("Holdback Capacity" custom field). Until the
        # Close→AEGIS sync of that field lands (commit-2 in this series),
        # default to 25% of monthly revenue — the MCA-shop convention for
        # the max sustainable debt-service load on a "clean" cashflow.
        # Documented in src/aegis/scoring_v2/offer.py.
        offer = compute_offer(
            true_revenue_monthly=latest_analysis.monthly_revenue,
            holdback_capacity_monthly=(latest_analysis.monthly_revenue * Decimal("0.25")),
            mca_stack=mca_stack,
        )
        pattern_analysis = _dossier_pattern_analysis(latest_analysis, latest_transactions)
        pattern_analysis_for_view = pattern_analysis
        # Migration 034 — scoring requires a finalized merchant.
        # Skip the score panel + statement_coverage build for
        # provisional / needs_manual_naming; the dossier still
        # renders, just without the score-derived sections.
        if items and merchant.is_finalized:
            score_input = _score_input_multi_month(
                merchant, items, pattern_analysis=pattern_analysis
            )
            # Shadow-only revenue/ADB/NSF trend chips. Reads
            # score_input.monthly_breakdown (already populated by the
            # multi-month scorer); single-month or empty inputs collapse
            # to all-flat per the function's contract.
            revenue_trends = compute_revenue_trends(score_input.monthly_breakdown)
            # U33 — feed Track A integrity verdict + Track B band into the
            # scorer so ``AEGIS_SCORING_ENGINE=track_abc`` has live inputs.
            # Under the default ``legacy`` engine these are ignored and
            # the scorer is byte-identical to pre-U30.
            track_a_verdict, track_b_band = compute_score_deal_track_inputs(
                documents=all_docs,
                list_transactions=docs.list_transactions,
                analyses_by_doc=analyses_by_doc,
                merchant_id=merchant_id,
                industry_tier=industry_risk_tier(merchant.industry_choice),
            )
            try:
                score_result = score_deal(
                    score_input,
                    ofac=ofac,
                    track_a_verdict=track_a_verdict,
                    track_b_band=track_b_band,
                )
            except OFACStaleError:
                score_result = None
            score_window = {
                "months_used": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "any_manual_review": any(d.parse_status == "manual_review" for d, _ in items),
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
        _hard_decline_reasons = list(score_result.hard_decline_reasons) if score_result else None
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
        ofac_status, ofac_match = _ofac_ribbon_status(ofac, merchant.business_name)
    else:
        ofac_status, ofac_match = ("not_consulted", None)

    from aegis.api.routes.findings import _compute_trend

    trend = _compute_trend(all_docs, docs)
    history = audit.list_for_subject(subject_type="merchant", subject_id=merchant_id, limit=20)
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
    _has_concentration_pattern = pattern_has_customer_concentration(pattern_analysis_for_view)

    # U18 — humanized shadow flags rolled up across the parser /
    # validation / scoring layers for the discreet "Operator review
    # signals (shadow mode)" details section below the verdict. None
    # of these affect tier / decline; the section is collapsed by
    # default and stays out of the way of hard-decline reasoning.
    shadow_signals = _collect_shadow_flags_for_dossier(
        latest_doc=latest_doc,
        pattern_analysis=pattern_analysis_for_view,
        score_result=score_result,
    )

    # U22 — merchant-level shadow signals persisted by the worker hook
    # in ``merchants_shadow_signals`` (migration 044). These are the
    # cross-statement / related-account signals U15 emitted at upload
    # time. We hand each row through the SAME U18 humanizer the
    # per-document signals use (``humanize_flag(code:detail)``) so the
    # dossier section reads consistently. The row also carries the raw
    # ``MerchantShadowSignalRecord`` so the template can show the
    # ``detected_at`` timestamp the per-document section lacks.
    merchant_shadow_signal_rows = shadow_signals_repo.list_by_merchant(
        merchant_id=merchant_id, limit=50
    )
    merchant_shadow_signals = _humanize_merchant_shadow_signals(merchant_shadow_signal_rows)

    # Build the unified A+B+C view alongside the existing score block.
    # Pure presentation; no decline-path impact. Step 2 of the scoring
    # redesign retires fraud_score and flips A/B/C live; this commit
    # only adds the surface.
    from aegis.scoring_v2.dossier_panel import build_unified_tracks_view

    unified_tracks = build_unified_tracks_view(
        documents=all_docs,
        list_transactions=docs.list_transactions,
        analyses_by_doc=analyses_by_doc,
        industry_tier=industry_risk_tier(merchant.industry_choice),
    )

    # Funder-note submission history (newest-first). Dossier renders a
    # compact list of every Submit-to-Funder click for this merchant
    # with status + offered terms when a funder has responded. Empty
    # list when nothing has been submitted yet (the template handles
    # the empty-state copy).
    funder_note_submissions = funder_note_subs.list_for_merchant(merchant_id=merchant_id, limit=50)

    # Inline § 4 funder-matching panel (2026-06-19) — same card list the
    # standalone /match panel renders. Built via ``_build_match_cards`` so
    # the two surfaces stay in lockstep on color rule, historical-approval
    # boost, and sort order. Skipped when the merchant isn't yet scoreable
    # (no ``score_result`` or no ``score_input``) — § 4 falls back to its
    # empty-state copy. The list is also the source of ``top_matched_funder``
    # for the stipulations evaluator below; previously the dossier ran its
    # own match loop here, which drifted from /match whenever the matcher
    # gained a parameter.
    matched_funders_cards: list[dict[str, Any]] = []
    top_matched_funder: FunderRow | None = None
    stips_result: StipsResult | None = None
    if score_result is not None and score_input is not None:
        matched_funders_cards = _build_match_cards(
            merchant=merchant,
            score_input=score_input,
            score_result=score_result,
            funder_repo=funder_repo,
            merchants_repo=merchants,
            docs=docs,
            funder_note_subs=funder_note_subs,
            snapshot=snapshot,
            offer=offer,
            bank_warning=_fintech_bank_warning_from_flags(
                list(latest_doc.all_flags) if latest_doc is not None else None
            ),
        )
        if matched_funders_cards:
            top_matched_funder = funder_repo.get(UUID(matched_funders_cards[0]["funder_id"]))
            stips_result = evaluate_stips(top_matched_funder, merchant)

    # Most-recent funder reply per funder (audit-derived). The /match
    # panel surfaces these on each card; the inline § 4 panel mirrors
    # that affordance so the operator sees the reply chip without
    # navigating to /match.
    matched_funder_responses = _latest_funder_responses(audit, merchant_id)

    # Per-funder submit history. Empty dict when the operator hasn't yet
    # clicked Submit on any funder. The inline § 4 panel uses this to
    # render a "Submitted ✓" chip on funders the operator already posted
    # so the per-funder buttons don't look re-submittable on dossier reload.
    submitted_funder_ids = {
        str(s.funder_id) for s in funder_note_submissions if s.funder_id is not None
    }

    # Feature C — operator notes panel. Newest-first list of timestamped
    # cards rendered above the chips section. Bounded to 50 rows to keep
    # the dossier read cost predictable when a merchant has accumulated
    # many notes over time.
    operator_notes = merchants.list_notes(merchant_id=merchant_id, limit=50)

    # Override-modal context (mp Phase 10 / migration 072). Per-document
    # pattern codes power the "false-positive per pattern" checkbox set
    # so the operator can mark exactly which detectors fired wrongly on
    # this deal. Includes the live ``patterns`` list (decision-boundary
    # inputs) and the shadow list (informational signals). Empty list
    # for legacy docs without a cached pattern_analysis. Deduplicated
    # while preserving first-seen order so the modal renders a
    # deterministic ordering regardless of how the parser surfaced them.
    override_pattern_codes: list[str] = []
    _seen_codes: set[str] = set()
    if pattern_analysis_for_view is not None:
        for _p in list(pattern_analysis_for_view.patterns) + list(
            pattern_analysis_for_view.shadow_patterns
        ):
            if _p.code not in _seen_codes:
                _seen_codes.add(_p.code)
                override_pattern_codes.append(_p.code)
    # Latest decision_id for this merchant — the override row pins to
    # it when present. None for docs without a decisions row (older
    # parses, or never-scored docs). The modal includes a hidden
    # decision_id field that the route accepts as optional.
    override_latest_decision_id: str | None = None
    if score_result is not None and latest_doc is not None:
        latest_decision = snapshot.find_latest_for_merchant(
            merchant_id, deal_ids=[d.id for d in all_docs]
        )
        if latest_decision is not None:
            override_latest_decision_id = str(latest_decision.id)
    # The button is gated on parse_status in {proceed, decline} per the
    # task spec — manual_review is already operator-driven and pending /
    # error / review don't have a recommendation to override yet.
    show_override_button = latest_doc is not None and latest_doc.parse_status in {
        "proceed",
        "decline",
    }

    # Plain-English deal summary card (2026-06-18 redesign). Rule-based,
    # no LLM call — produces the headline + body + flags the team reads
    # first at the top of the dossier. None when the deal isn't yet
    # scoreable (no document on file, no balance_health, no MCA stack);
    # the template renders an empty-state in that branch.
    deal_summary = None
    funder_narrative = ""
    if score_result is not None and balance_health is not None and mca_stack is not None:
        from aegis.scoring_v2.deal_summary import (
            CloseContext,
            generate_deal_summary,
            generate_funder_narrative,
        )

        _close_ctx = CloseContext(
            lead_description=merchant.close_lead_description,
            notes_summary=merchant.close_notes_summary,
            call_transcripts=merchant.close_call_transcripts,
        )
        deal_summary = generate_deal_summary(
            merchant=merchant,
            score_result=score_result,
            mca_stack=mca_stack,
            balance_health=balance_health,
            close_context=_close_ctx,
        )
        # Bedrock-generated 3-4 sentence funder-facing narrative. Empty
        # string when Bedrock is unavailable — the template renders a
        # "narrative not available" state and the Submit path falls
        # back to format_funder_note alone.
        funder_narrative = generate_funder_narrative(
            merchant=merchant,
            score_result=score_result,
            mca_stack=mca_stack,
            balance_health=balance_health,
            offer=offer,
            close_context=_close_ctx,
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
            "mca_stack": mca_stack,
            "balance_health": balance_health,
            "offer": offer,
            "state_tier": state_tier_dossier,
            "ofac_status": ofac_dossier_status,
            "ofac_match": ofac_match,
            "trend": trend,
            "history": history,
            "close_last_orchestration_capped": close_last_orchestration_capped,
            "unified_tracks": unified_tracks,
            "shadow_signals": shadow_signals,
            "merchant_shadow_signals": merchant_shadow_signals,
            "revenue_trends": revenue_trends,
            "funder_note_submissions": funder_note_submissions,
            "operator_notes": operator_notes,
            "operator_note_max_chars": MERCHANT_NOTE_MAX_CHARS,
            "deal_summary": deal_summary,
            "funder_narrative": funder_narrative,
            "doc_checklist": {
                "voided_check_on_file": merchant.voided_check_on_file,
                "drivers_license_on_file": merchant.drivers_license_on_file,
                "bank_statements_months": merchant.bank_statements_months,
            },
            "stips_result": (stips_result.model_dump() if stips_result else None),
            "top_matched_funder_name": (top_matched_funder.name if top_matched_funder else None),
            "matched_funders": matched_funders_cards,
            "matched_funder_responses": matched_funder_responses,
            "submitted_funder_ids": submitted_funder_ids,
            "override_pattern_codes": override_pattern_codes,
            "override_latest_decision_id": override_latest_decision_id,
            "show_override_button": show_override_button,
        },
    )


@router.get("/merchants/{merchant_id}/dossier.pdf")
async def merchant_dossier_pdf(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    funder_note_subs: Annotated[
        FunderNoteSubmissionRepository,
        Depends(get_funder_note_submission_repository),
    ],
) -> Response:
    """Downloadable PDF dossier of the merchant.

    Same data as the HTML dossier at ``/ui/merchants/{id}`` but laid out
    for paper: US Letter, page-break controls, no HTMX, no sidebar,
    system fonts. Always renders the *default* bundle (most-populated
    bank/last4 pair); operators switching bundles on the dashboard get
    the on-screen view, not a separate PDF per bundle.

    ``funder_repo`` + ``funder_note_subs`` feed the top-funder-matches
    section + the submission-history section so the funder-facing PDF
    carries the same chips the on-screen dossier shows the operator.
    """
    try:
        merchant = merchants.get(merchant_id)
    except MerchantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    context = _build_pdf_dossier_context(
        merchant,
        docs,
        ofac,
        funder_repo=funder_repo,
        funder_note_subs=funder_note_subs,
    )
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
    *,
    funder_repo: FunderRepository | None = None,
    funder_note_subs: FunderNoteSubmissionRepository | None = None,
) -> dict[str, Any]:
    """Build the print-template context (parity with merchant_detail's context).

    The print template needs the same data set the on-screen dossier
    renders so the funder-facing PDF carries the same industry-tier,
    trend, stacking, balance-health, offer, top-funder-matches, and
    submission-history chips the operator already trusts seeing on
    screen.

    ``funder_repo`` + ``funder_note_subs`` are optional. When None the
    top-funder-matches and submission-history sections render as empty
    states (template handles graceful omission) — relevant for the
    submit-to-funders flow which historically built the PDF without
    these repos.
    """
    all_docs = docs.list_documents(merchant_id=merchant.id, limit=50)
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in all_docs])
    documents_table: list[dict[str, Any]] = [
        {"document": d, "analysis": analyses_by_doc.get(d.id)} for d in all_docs
    ]

    latest_doc = all_docs[0] if all_docs else None
    latest_analysis = analyses_by_doc.get(latest_doc.id) if latest_doc else None

    score_result = None
    score_input: ScoreInput | None = None
    score_window = None
    statement_coverage: dict[str, Any] | None = None
    stacking = None
    mca_stack = None
    balance_health = None
    offer = None
    revenue_trends = None
    pattern_cards: list[Any] = []
    pattern_analysis_for_view: Any = None

    if latest_doc is not None and latest_analysis is not None:
        all_items = _collect_analyzed_for_merchant(docs, merchant.id, window=999, bundle=None)
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
        mca_stack = aggregate_mca_stack(
            transactions=latest_transactions,
            monthly_revenue=latest_analysis.monthly_revenue,
            period_days=latest_analysis.statement_days,
        )
        balance_health = compute_balance_health(
            transactions=latest_transactions,
            period_days=latest_analysis.statement_days,
        )
        # ``holdback_capacity_monthly`` is operator-confirmed on the Close
        # Opportunity ("Holdback Capacity" custom field). Until the
        # Close→AEGIS sync of that field lands (commit-2 in this series),
        # default to 25% of monthly revenue — the MCA-shop convention for
        # the max sustainable debt-service load on a "clean" cashflow.
        # Documented in src/aegis/scoring_v2/offer.py.
        offer = compute_offer(
            true_revenue_monthly=latest_analysis.monthly_revenue,
            holdback_capacity_monthly=(latest_analysis.monthly_revenue * Decimal("0.25")),
            mca_stack=mca_stack,
        )
        pattern_analysis = _dossier_pattern_analysis(latest_analysis, latest_transactions)
        pattern_analysis_for_view = pattern_analysis

        # Migration 034 — same is_finalized scoring gate as the
        # matched-funders dossier branch above.
        if items and merchant.is_finalized:
            score_input = _score_input_multi_month(
                merchant, items, pattern_analysis=pattern_analysis
            )
            # Shadow-only revenue / ADB / NSF trend chips — same source
            # as the on-screen dossier. Single-month inputs collapse to
            # all-flat per the function's contract.
            revenue_trends = compute_revenue_trends(score_input.monthly_breakdown)
            # U33 — same Track A/B feed as the HTML dossier branch above.
            track_a_verdict, track_b_band = compute_score_deal_track_inputs(
                documents=all_docs,
                list_transactions=docs.list_transactions,
                analyses_by_doc=analyses_by_doc,
                merchant_id=merchant.id,
                industry_tier=industry_risk_tier(merchant.industry_choice),
            )
            try:
                score_result = score_deal(
                    score_input,
                    ofac=ofac,
                    track_a_verdict=track_a_verdict,
                    track_b_band=track_b_band,
                )
            except OFACStaleError:
                score_result = None
            score_window = {
                "months_used": len(items),
                "period_start": score_input.statement_period_start,
                "period_end": score_input.statement_period_end,
                "any_manual_review": any(d.parse_status == "manual_review" for d, _ in items),
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
        _hard_decline_reasons = list(score_result.hard_decline_reasons) if score_result else None
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
        ofac_status_raw, ofac_match = _ofac_ribbon_status(ofac, merchant.business_name)
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

    # Unified A+B+C view — drives the industry-tier chip on the PDF.
    # Same source as the on-screen dossier (``build_unified_tracks_view``).
    from aegis.scoring_v2.dossier_panel import build_unified_tracks_view

    unified_tracks = build_unified_tracks_view(
        documents=all_docs,
        list_transactions=docs.list_transactions,
        analyses_by_doc=analyses_by_doc,
        industry_tier=industry_risk_tier(merchant.industry_choice),
    )

    # Top 3 funder matches. Only computed when a finalized merchant has a
    # score_result AND the caller threaded a funder repo. Sort by
    # ``match_score`` descending and slice — mirrors the on-screen
    # matched-funders panel. The first ``TierMatch`` with ``qualifies=True``
    # (if any) labels the qualifying tier on the dossier line.
    top_matched_funders: list[dict[str, Any]] = []
    if funder_repo is not None and score_input is not None and score_result is not None:
        _matched: list[FunderMatch] = []
        for _f in funder_repo.list_active():
            _m = match_funder(_f, score_input, score_result, offer=offer)
            if _m is not None:
                _matched.append(_m)
        _matched.sort(key=lambda fm: fm.match_score, reverse=True)
        for fm in _matched[:3]:
            qualifying_tier_name: str | None = None
            for tm in fm.tier_matches:
                if tm.qualifies:
                    qualifying_tier_name = tm.tier_name
                    break
            top_matched_funders.append(
                {
                    "funder_id": fm.funder_id,
                    "funder_name": fm.funder_name,
                    "match_score": fm.match_score,
                    "qualifying_tier": qualifying_tier_name,
                }
            )

    # Last 3 funder-note submissions (newest first). Empty list when no
    # funder_note_subs repo wired or no submissions yet — template
    # renders the empty-state line gracefully.
    funder_note_submissions: list[Any] = []
    if funder_note_subs is not None:
        funder_note_submissions = funder_note_subs.list_for_merchant(
            merchant_id=merchant.id, limit=3
        )

    return {
        "merchant": merchant,
        "document": latest_doc,
        "analysis": latest_analysis,
        "documents": documents_table,
        "score_result": score_result,
        "score_window": score_window,
        "statement_coverage": statement_coverage,
        "stacking": stacking,
        "mca_stack": mca_stack,
        "balance_health": balance_health,
        "offer": offer,
        "pattern_cards": pattern_cards,
        "has_concentration_pattern": pattern_has_customer_concentration(pattern_analysis_for_view),
        "state_tier": state_tier_dossier,
        "ofac_status": ofac_dossier_status,
        "ofac_match": ofac_match,
        "unified_tracks": unified_tracks,
        "revenue_trends": revenue_trends,
        "top_matched_funders": top_matched_funders,
        "funder_note_submissions": funder_note_submissions,
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


# ---------------------------------------------------------------------------
# Helpers private to merchants
# ---------------------------------------------------------------------------


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


def _criteria_comparison(funder: FunderRow, score_input: ScoreInput) -> list[dict[str, Any]]:
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
        excluded = any(score_input.industry_naics.startswith(x) for x in funder.excluded_industries)
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


def _is_marketplace_funder(funder: FunderRow) -> bool:
    """Detect a marketplace / aggregator funder with no published criteria.

    Three funders in the live catalog (Splash Advance, Big Think Capital,
    Bizi Connect, 2026-06-20) are correctly active but carry no
    underwriting criteria — they don't underwrite directly, they're
    aggregator routes. The match panel still produces a card for them,
    but the underlying ``match_score`` is meaningless because the
    matcher had no thresholds to evaluate. Surface a "Marketplace" badge
    in place of (or alongside) the score so the team doesn't read the
    card as a normal criteria-based match.

    Heuristic: ``active=True`` AND all three of the most-load-bearing
    operator-set criteria are unset. Stops short of treating "active +
    one or two criteria set" as a marketplace — that's a partial entry
    the operator hasn't finished, a different problem.
    """
    return (
        funder.active
        and funder.min_monthly_revenue is None
        and funder.min_credit_score is None
        and funder.max_positions is None
    )


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
        # Per-tier qualification matrix (U28). Empty when the funder has
        # no tiers JSONB populated (most brokers/affiliates) — template
        # suppresses the tier block in that case.
        "tier_matches": match.tier_matches,
        # Sprint 4 -- historical approval rate for similar prior
        # submissions (same merchant industry tier, same AEGIS score
        # tier, last 90 days). None when the matcher had no data, when
        # the cell fell below the 5-submission floor, or when the
        # route's index build hit an outage. Template renders as a
        # short "track record" qualifier on the card when present.
        "historical_approval_rate": match.historical_approval_rate,
        # Marketplace / aggregator flag — see ``_is_marketplace_funder``.
        # Template surfaces a "Marketplace" badge in place of the score
        # so the team doesn't misread a criteria-less card as a real
        # criteria-based match.
        "is_marketplace": _is_marketplace_funder(funder),
    }


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


def _ofac_ribbon_status(ofac: OFACClient | None, business_name: str) -> tuple[str, bool | None]:
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


__all__ = [
    "_FUNDER_RESPONSE_STATUSES",
    "MerchantShadowSignalView",
    "_build_pdf_dossier_context",
    "_close_orchestration_last_capped",
    "_collect_shadow_flags_for_dossier",
    "_criteria_comparison",
    "_dossier_pattern_analysis",
    "_find_latest_for_merchant",
    "_humanize_merchant_shadow_signals",
    "_latest_funder_responses",
    "_match_card",
    "_maybe_render_dossier_pdf",
    "_merchant_form_error",
    "_ofac_ribbon_status",
    "_parse_funder_ids",
    "_state_tier",
    "_txs_to_rows",
    "router",
]
