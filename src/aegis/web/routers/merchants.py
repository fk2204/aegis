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

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    get_funder_repository,
    get_merchant_repository,
    get_merchant_shadow_signal_repository,
    get_ofac_client,
    get_repository,
    get_submission_repository,
)
from aegis.audit import AuditLog
from aegis.close.orchestration import enqueue_close_orchestration
from aegis.compliance.states import STATES
from aegis.funders.models import FunderRow
from aegis.funders.repository import (
    FunderNotFoundError,
    FunderRepository,
)
from aegis.merchants.models import MerchantRow
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
from aegis.scoring_v2.mca_stack import aggregate_mca_stack
from aegis.scoring_v2.score_deal_inputs import compute_score_deal_track_inputs
from aegis.storage import (
    AnalysisRow,
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
# Submit to funders (CSV / ZIP download + audit row + submissions rows).
# ---------------------------------------------------------------------------


@router.post("/merchants/{merchant_id}/submit", response_model=None)
async def merchant_submit_to_funders(
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    funder_repo: Annotated[FunderRepository, Depends(get_funder_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    submissions_repo: Annotated[SubmissionRepository, Depends(get_submission_repository)],
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
    proposed_factor = (
        score_result.recommended_factor_rate
        if score_result.recommended_factor_rate > Decimal("1")
        else score_input.requested_factor
    )
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
# Merchant detail (HTML dossier) + PDF dossier + findings.csv
# ---------------------------------------------------------------------------


@router.get("/merchants/{merchant_id}", response_class=HTMLResponse)
async def merchant_detail(
    request: Request,
    merchant_id: UUID,
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    ofac: Annotated[OFACClient | None, Depends(get_ofac_client)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    shadow_signals_repo: Annotated[
        MerchantShadowSignalRepository,
        Depends(get_merchant_shadow_signal_repository),
    ],
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
    mca_stack = None
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
            # U33 — feed Track A integrity verdict + Track B band into the
            # scorer so ``AEGIS_SCORING_ENGINE=track_abc`` has live inputs.
            # Under the default ``legacy`` engine these are ignored and
            # the scorer is byte-identical to pre-U30.
            track_a_verdict, track_b_band = compute_score_deal_track_inputs(
                documents=all_docs,
                list_transactions=docs.list_transactions,
                analyses_by_doc=analyses_by_doc,
                merchant_id=merchant_id,
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
            "state_tier": state_tier_dossier,
            "ofac_status": ofac_dossier_status,
            "ofac_match": ofac_match,
            "trend": trend,
            "history": history,
            "close_last_orchestration_capped": close_last_orchestration_capped,
            "unified_tracks": unified_tracks,
            "shadow_signals": shadow_signals,
            "merchant_shadow_signals": merchant_shadow_signals,
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
    mca_stack = None
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
        pattern_analysis = _dossier_pattern_analysis(latest_analysis, latest_transactions)
        pattern_analysis_for_view = pattern_analysis

        # Migration 034 — same is_finalized scoring gate as the
        # matched-funders dossier branch above.
        if items and merchant.is_finalized:
            score_input = _score_input_multi_month(
                merchant, items, pattern_analysis=pattern_analysis
            )
            # U33 — same Track A/B feed as the HTML dossier branch above.
            track_a_verdict, track_b_band = compute_score_deal_track_inputs(
                documents=all_docs,
                list_transactions=docs.list_transactions,
                analyses_by_doc=analyses_by_doc,
                merchant_id=merchant.id,
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
        "pattern_cards": pattern_cards,
        "has_concentration_pattern": pattern_has_customer_concentration(pattern_analysis_for_view),
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
