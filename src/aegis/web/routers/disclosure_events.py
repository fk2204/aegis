"""Disclosure-events sub-router — operator triage for disclosure_render_events.

Routes:
  * ``GET /ui/disclosure-events``              — list view, filterable
  * ``GET /ui/disclosure-events/{event_id}``   — per-event detail

(U21, persistence in migration 042 / U16). Read-only — render events
are diagnostic only. The operator cannot "resolve" a row in place; once
the deal renders cleanly next time, a new ``ok`` event is recorded
alongside the failing one.

Per .claude/rules/compliance.md SCOPE NOTE: these are AEGIS internal
pre-flight records. The funder owns regulator-facing disclosure
issuance — the page banner makes that explicit so the operator does
not mis-read an ``apr_compute_failed`` as a regulator-side incident.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Final, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_disclosure_render_event_repository
from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    RENDER_EVENT_STATUS_OK,
    DisclosureRenderEventRepository,
)
from aegis.web._templates import templates

router = APIRouter()


_DISCLOSURE_EVENTS_DEFAULT_WINDOW_DAYS: Final[int] = 14
_DISCLOSURE_EVENTS_MAX_WINDOW_DAYS: Final[int] = 365
_DISCLOSURE_EVENTS_LIMIT: Final[int] = 500

# Status filter values surfaced in the dropdown. The empty string
# represents "all statuses" (a route-level convention — the repo's
# list_in_window accepts ``status=None``).
_DISCLOSURE_EVENTS_STATUS_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    (RENDER_EVENT_STATUS_NEEDS_REVIEW, "needs_review"),
    (RENDER_EVENT_STATUS_APR_FAILED, "apr_compute_failed"),
    (RENDER_EVENT_STATUS_OK, "ok"),
    ("", "all"),
)


def _resolve_disclosure_events_window(
    from_str: str | None,
    to_str: str | None,
) -> tuple[date, date]:
    """Parse ``?from=&to=``. Defaults to last 14 days. Clamps to 365.

    Raises ``ValueError`` on malformed input — the route turns the
    failure into a 400 with the parser message.
    """
    from datetime import timedelta as _timedelta

    today = datetime.now(UTC).date()
    parsed_to = (
        datetime.fromisoformat(to_str.strip()).date() if to_str else today
    )
    parsed_from = (
        datetime.fromisoformat(from_str.strip()).date()
        if from_str
        else parsed_to
        - _timedelta(days=_DISCLOSURE_EVENTS_DEFAULT_WINDOW_DAYS)
    )
    if parsed_to < parsed_from:
        raise ValueError(
            f"to_date {parsed_to.isoformat()} is earlier than from_date "
            f"{parsed_from.isoformat()}"
        )
    span = (parsed_to - parsed_from).days
    if span > _DISCLOSURE_EVENTS_MAX_WINDOW_DAYS:
        parsed_from = parsed_to - _timedelta(
            days=_DISCLOSURE_EVENTS_MAX_WINDOW_DAYS
        )
    return parsed_from, parsed_to


@router.get("/disclosure-events", response_class=HTMLResponse)
async def disclosure_events_view(
    request: Request,
    render_events_repo: Annotated[
        DisclosureRenderEventRepository,
        Depends(get_disclosure_render_event_repository),
    ],
    status_param: Annotated[
        str,
        Query(
            alias="status",
            description=(
                "Filter by render-event status. One of ``needs_review`` / "
                "``apr_compute_failed`` / ``ok`` / ``all`` (empty string). "
                "Default: ``needs_review`` so the triage queue is the "
                "first thing the operator sees."
            ),
        ),
    ] = RENDER_EVENT_STATUS_NEEDS_REVIEW,
    from_: Annotated[
        str | None,
        Query(
            alias="from",
            description=(
                "Window start (YYYY-MM-DD). Defaults to today minus 14 days."
            ),
        ),
    ] = None,
    to: Annotated[
        str | None,
        Query(
            description=("Window end (YYYY-MM-DD). Defaults to today."),
        ),
    ] = None,
) -> HTMLResponse:
    """Render-event triage page.

    Default view: ``status=needs_review`` over the last 14 days. The
    table mirrors the renewals.html.j2 idiom — one row per event with
    a "View details →" link to the per-event subroute.
    """
    try:
        from_date, to_date = _resolve_disclosure_events_window(from_, to)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid date range: {exc}",
        ) from exc

    # Empty string → "all" filter; validate any non-empty value lands in
    # the known set so a bad query param surfaces as 400 rather than a
    # silent empty page.
    allowed = {opt[0] for opt in _DISCLOSURE_EVENTS_STATUS_OPTIONS}
    if status_param not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"invalid status {status_param!r}; expected one of "
                f"{sorted(allowed)!r}"
            ),
        )
    effective_status: str | None = status_param if status_param else None

    try:
        rows = render_events_repo.list_in_window(
            from_date=from_date,
            to_date=to_date,
            status=effective_status,
            limit=_DISCLOSURE_EVENTS_LIMIT,
        )
    except Exception:
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "disclosure_events.fetch_failed window=%s..%s status=%s",
            from_date,
            to_date,
            status_param,
        )
        rows = []

    status_options = [
        {"value": value, "label": label}
        for value, label in _DISCLOSURE_EVENTS_STATUS_OPTIONS
    ]

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "disclosure_events.html.j2",
            {
                "active": "DisclosureEvents",
                "rows": rows,
                "status": status_param,
                "from_date": from_date,
                "to_date": to_date,
                "status_options": status_options,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )


@router.get("/disclosure-events/{event_id}", response_class=HTMLResponse)
async def disclosure_event_detail(
    request: Request,
    event_id: UUID,
    render_events_repo: Annotated[
        DisclosureRenderEventRepository,
        Depends(get_disclosure_render_event_repository),
    ],
) -> HTMLResponse:
    """Detail subroute — every column on one render event.

    Read-only. Render events have no triage state machine (the next
    clean APR converge produces a new ``ok`` row rather than mutating
    the failing one), so this page does not surface any forms.
    """
    import json as _json

    record = render_events_repo.get(event_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no render event with id={event_id}",
        )

    details_json = (
        _json.dumps(record.details, indent=2, default=str)
        if record.details is not None
        else "(none)"
    )
    metadata_json = (
        _json.dumps(record.metadata, indent=2, default=str)
        if record.metadata is not None
        else "(none)"
    )

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "disclosure_event_detail.html.j2",
            {
                "active": "DisclosureEvents",
                "record": record,
                "details_json": details_json,
                "metadata_json": metadata_json,
                "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            },
        ),
    )
