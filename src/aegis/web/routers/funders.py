"""Funders sub-router — list / create (manual + PDF import) / detail / modals.

Routes:
  * ``GET  /ui/funders``                              — list active funders
  * ``GET  /ui/funders/new``                          — manual create form
  * ``POST /ui/funders/new``                          — create funder
  * ``GET  /ui/funders/import``                       — PDF/PNG import form
  * ``POST /ui/funders/import``                       — extract via Bedrock + preview
  * ``POST /ui/funders/import/save``                  — save the previewed extraction
  * ``GET  /ui/funders/{funder_id}``                  — detail view
  * ``GET  /ui/funders/{funder_id}/submit-modal``     — HTMX merchant-picker modal
  * ``GET  /ui/funders/{funder_id}/reextract-modal``  — HTMX re-extract modal
  * ``POST /ui/funders/{funder_id}/reextract``        — re-run LLM extraction
  * ``POST /ui/funders/{funder_id}/operator-notes``   — save operator note

Extracted from ``router.py`` during R4.1. The shared form-parsers
(``_decimal_or_none`` / ``_int_or_none`` / ``_sha256_hex``) were lifted
to ``aegis.web._router_helpers`` so the still-resident merchants routes
keep consuming the same code paths after this split.
"""

from __future__ import annotations

import urllib.parse
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from aegis.api.deps import (
    get_audit,
    get_deal_repository,
    get_funder_repository,
    get_llm,
)
from aegis.audit import AuditLog
from aegis.deals.repository import DealRepository
from aegis.funders.extract import (
    FunderExtractionError,
    extract_funder_guidelines,
    extract_funder_guidelines_from_image,
    merge_extractions,
)
from aegis.funders.models import FunderRow, FunderTier
from aegis.funders.repository import (
    FunderNotFoundError,
    FunderRepository,
)
from aegis.llm import LLMClient
from aegis.ops.operators import resolve_operator_email
from aegis.web._router_helpers import (
    _decimal_or_none,
    _int_or_none,
    _sha256_hex,
)
from aegis.web._templates import templates

router = APIRouter()


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


# Soft cap for operator notes so a stray paste doesn't dump a megabyte
# into the funder row. 10K chars = ~5 long paragraphs; tighten or
# loosen later if real usage shows we need it.
_OPERATOR_NOTES_MAX_CHARS = 10_000


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
