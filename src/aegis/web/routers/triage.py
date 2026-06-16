"""Triage + shadow-signals sub-router (U24).

Routes:
  * ``GET /ui/triage``           — three-tile aggregate triage backlog
  * ``GET /ui/shadow-signals``   — cross-merchant shadow-signal view

Read-only. Operator triage actions happen on the linked-out queues
(scoring disagreements via the corpus runner; disclosure render
events via ``/ui/disclosure-events``; per-merchant signals via the
merchant dossier).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_disclosure_render_event_repository,
    get_merchant_shadow_signal_repository,
    get_scoring_disagreement_repository,
)
from aegis.compliance.render_events import DisclosureRenderEventRepository
from aegis.deals.triage_analytics import (
    MAX_TRIAGE_WINDOW_DAYS,
    compute_triage_backlog,
    resolve_triage_window,
)
from aegis.merchants.shadow_signals import MerchantShadowSignalRepository
from aegis.scoring_v2.shadow_disagreements import (
    ScoringDisagreementRepository,
)
from aegis.web._flag_labels import humanize_flag
from aegis.web._templates import templates

router = APIRouter()


@router.get("/triage", response_class=HTMLResponse)
async def triage_view(
    request: Request,
    disagreements_repo: Annotated[
        ScoringDisagreementRepository,
        Depends(get_scoring_disagreement_repository),
    ],
    render_events_repo: Annotated[
        DisclosureRenderEventRepository,
        Depends(get_disclosure_render_event_repository),
    ],
    shadow_signals_repo: Annotated[
        MerchantShadowSignalRepository,
        Depends(get_merchant_shadow_signal_repository),
    ],
    days: Annotated[
        int | None,
        Query(
            description=(
                "Window length in days for render-events + shadow-signals "
                "tallies. Default 30, max 365. Scoring disagreements are "
                "filtered by ``triaged_at IS NULL`` only — they do not "
                "honor this window because the corpus run cadence is "
                "independent of the calendar window."
            ),
            ge=1,
            le=MAX_TRIAGE_WINDOW_DAYS,
        ),
    ] = None,
) -> HTMLResponse:
    """Operator triage backlog — three KPI tiles + deep-links.

    Each tile shows the queue count + a breakdown + a "Triage →" link
    to the surface where the operator resolves the rows. Empty backlog
    renders a single "no triage pending" banner.
    """
    window = resolve_triage_window(days)
    backlog = compute_triage_backlog(
        disagreements_repo=disagreements_repo,
        render_events_repo=render_events_repo,
        shadow_signals_repo=shadow_signals_repo,
        window=window,
    )
    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "triage.html.j2",
            {
                "active": "Portfolio",
                "backlog": backlog,
                "window_days": window.days,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )


_SHADOW_SIGNALS_DEFAULT_WINDOW_DAYS: Final[int] = 30
_SHADOW_SIGNALS_LIMIT: Final[int] = 500


@router.get("/shadow-signals", response_class=HTMLResponse)
async def shadow_signals_view(
    request: Request,
    shadow_signals_repo: Annotated[
        MerchantShadowSignalRepository,
        Depends(get_merchant_shadow_signal_repository),
    ],
    code: Annotated[
        str | None,
        Query(
            description=(
                "Optional ``signal_code`` filter. Matches literal-equal. Empty / None → all codes."
            ),
        ),
    ] = None,
    merchant: Annotated[
        str | None,
        Query(
            description=(
                "Optional ``merchant_id`` (UUID) filter. Empty / None → "
                "all merchants. Malformed UUID surfaces as 400."
            ),
        ),
    ] = None,
    days: Annotated[
        int | None,
        Query(
            description=("Window length in days. Default 30, max 365."),
            ge=1,
            le=MAX_TRIAGE_WINDOW_DAYS,
        ),
    ] = None,
) -> HTMLResponse:
    """Cross-merchant shadow-signal view.

    Default window: last 30 days, no filters → every signal across
    every merchant. Filters apply server-side on the Supabase backend
    via ``list_in_window``.
    """
    effective_days = days if days is not None else _SHADOW_SIGNALS_DEFAULT_WINDOW_DAYS
    window = resolve_triage_window(effective_days)

    # Trim whitespace + treat empty strings as "no filter".
    cleaned_code: str | None = code.strip() if code else None
    if cleaned_code == "":
        cleaned_code = None

    cleaned_merchant_raw = merchant.strip() if merchant else None
    if cleaned_merchant_raw == "":
        cleaned_merchant_raw = None
    merchant_uuid: UUID | None = None
    if cleaned_merchant_raw is not None:
        try:
            merchant_uuid = UUID(cleaned_merchant_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid merchant_id {cleaned_merchant_raw!r}: {exc}",
            ) from exc

    try:
        rows = shadow_signals_repo.list_in_window(
            from_date=window.from_date,
            to_date=window.to_date,
            signal_code=cleaned_code,
            merchant_id=merchant_uuid,
            limit=_SHADOW_SIGNALS_LIMIT,
        )
    except Exception:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "shadow_signals.fetch_failed window=%s..%s code=%s merchant=%s",
            window.from_date,
            window.to_date,
            cleaned_code,
            merchant_uuid,
        )
        rows = []

    # Build a (record, humanized) pairing so the template can render the
    # U18 title without coupling the template to the humanizer import.
    humanized_rows = [
        {
            "record": r,
            "humanized": humanize_flag(
                f"{r.signal_code}:{r.detail}" if r.detail else r.signal_code
            ),
        }
        for r in rows
    ]

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "shadow_signals.html.j2",
            {
                "active": "Portfolio",
                "rows": humanized_rows,
                "from_date": window.from_date,
                "to_date": window.to_date,
                "window_days": window.days,
                "filter_code": cleaned_code or "",
                "filter_merchant": cleaned_merchant_raw or "",
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )
