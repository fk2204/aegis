"""Bank coverage dashboard — single-page admin surface.

Surfaces the gap between banks the parser has seen and banks the
operator has armed with extraction hints, so the operator can act on
coverage gaps (bump parse counts to unlock auto-hints, or trigger
generation for banks that don't have hints yet).

Routes
------

* ``GET  /ui/bank-coverage``                                — full table
* ``POST /ui/bank-coverage/{bank_name}/bump-parse-count``  — increment
  successful_parses by 1; underwriter+; HTMX outerHTML swap target
  ``#bank-coverage-row-{normalized}``.
* ``POST /ui/bank-coverage/{bank_name}/generate-hints``    — enqueue
  auto-hint generation for one bank; underwriter+; same swap target.

The page does NOT duplicate ``/ui/bank-layouts`` — that one is the
hint-editing surface (textarea per bank). This one is the
coverage-gap surface (which banks need attention, sorted by gap size).

Sort order (coverage-gap descending):
  1. ``total_documents DESC``  — biggest data volume first
  2. ``hint_status`` rank      — No hints → Auto → Manual (banks with
     biggest gap surface first inside the same volume bucket)

Coordination flag
-----------------

The ``hints_source`` column on ``bank_layouts`` is added by a parallel
agent (auto-hints work). Read via ``getattr(row, "hints_source", None)``
so this code lands cleanly before that column exists. When the column
is absent, rows with non-empty ``extraction_hints`` are classified as
manual (the existing rows ARE operator-authored; conservative default).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Annotated, Any, NamedTuple, cast

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_bank_layout_repository,
    get_repository,
)
from aegis.audit import AuditLog
from aegis.bank_layouts import BankLayoutRepository, BankLayoutRow
from aegis.storage import DocumentRepository
from aegis.web._role_gate import underwriter_or_admin
from aegis.web._templates import templates

router = APIRouter()


# Cap on documents pulled for the per-bank rollup. ~100 deals/month
# x ~3 statements + processor PDFs over 10 years stays well under this
# cap; the limit is defensive against an unexpected blow-out without
# paying for a Postgres-side GROUP BY.
_DOCS_SCAN_CAP = 10_000


class _BankRow(NamedTuple):
    """One row in the bank-coverage table — view-model, not persisted."""

    bank_name: str
    bank_name_normalized: str  # lowercased for URL paths + DOM ids
    total_documents: int
    proceed_count: int
    successful_parses: int
    hints_source: str  # "manual" | "auto" | "mixed" | "—"
    hint_status: str  # "manual" | "auto" | "none"
    last_seen: datetime | None
    layout_row_id: str | None  # for audit subject_id


def _classify_hints(row: BankLayoutRow | None) -> tuple[str, str]:
    """Return (hint_status, hints_source) for one bank.

    ``hint_status`` is one of "manual" | "auto" | "none" — used for
    the sort key and the template's status pill.
    ``hints_source`` is the raw column value (or "—" when absent).
    """
    if row is None:
        return ("none", "—")
    hints = (row.extraction_hints or "").strip()
    if not hints:
        return ("none", "—")
    # ``hints_source`` lands when the auto-hints agent ships its
    # migration. Treat pre-column rows as manual since every existing
    # extraction_hints value was operator-authored via /ui/bank-layouts.
    raw_source = getattr(row, "hints_source", None)
    if raw_source in {"manual", "mixed"}:
        return ("manual", raw_source)
    if raw_source == "auto":
        return ("auto", "auto")
    return ("manual", "—")


# Sort rank: lower = earlier in the table. "none" surfaces first (biggest
# gap), then auto-hints, then manual hints. Within the same hint bucket,
# the higher-volume banks land first via the secondary sort.
_HINT_STATUS_RANK: dict[str, int] = {"none": 0, "auto": 1, "manual": 2}


def _build_rows(
    *,
    layouts: BankLayoutRepository,
    documents: DocumentRepository,
) -> list[_BankRow]:
    """Aggregate documents + analyses + bank_layouts into one row per bank.

    Banks present in ``bank_layouts`` but with zero documents still
    surface (total_documents=0). Banks present in documents but with no
    ``bank_layouts`` row also surface (successful_parses=0, no hints).
    Bank-name comparison is case-insensitive — Chase / CHASE / chase
    collapse to one row.
    """
    # ---- Pull bank_layouts first ----------------------------------------
    layout_rows = layouts.list_all()
    layout_by_lower: dict[str, BankLayoutRow] = {
        r.bank_name.strip().lower(): r for r in layout_rows
    }

    # ---- Pull every document + attach its bank_name via analyses --------
    docs = documents.list_documents(limit=_DOCS_SCAN_CAP)
    analyses = documents.get_analyses_by_document_ids([d.id for d in docs])

    per_bank_total: Counter[str] = Counter()
    per_bank_proceed: Counter[str] = Counter()
    per_bank_last_seen: dict[str, datetime] = {}
    per_bank_display: dict[str, str] = {}

    for doc in docs:
        analysis = analyses.get(doc.id)
        # Without an analysis the bank identity is unknown — group those
        # under "(no analysis yet)" so the operator sees the volume of
        # un-extracted docs in the coverage report rather than ignoring
        # them.
        bank = (analysis.bank_name if analysis else None) or "(no analysis yet)"
        key = bank.strip().lower()
        per_bank_total[key] += 1
        if doc.parse_status == "proceed":
            per_bank_proceed[key] += 1
        if key not in per_bank_last_seen or (
            doc.uploaded_at and doc.uploaded_at > per_bank_last_seen[key]
        ):
            per_bank_last_seen[key] = doc.uploaded_at
        # First-seen casing wins for the display string (matches the
        # case-collapse convention in bank_layouts).
        per_bank_display.setdefault(key, bank)

    # ---- Union the two key sets -----------------------------------------
    all_keys = set(per_bank_total) | set(layout_by_lower)
    rows: list[_BankRow] = []
    for key in all_keys:
        layout = layout_by_lower.get(key)
        hint_status, hints_source = _classify_hints(layout)
        display = per_bank_display.get(key) or (layout.bank_name if layout else key)
        last_seen = per_bank_last_seen.get(key)
        if layout and layout.last_seen and (last_seen is None or layout.last_seen > last_seen):
            last_seen = layout.last_seen
        rows.append(
            _BankRow(
                bank_name=display,
                bank_name_normalized=key,
                total_documents=per_bank_total.get(key, 0),
                proceed_count=per_bank_proceed.get(key, 0),
                successful_parses=layout.successful_parses if layout else 0,
                hints_source=hints_source,
                hint_status=hint_status,
                last_seen=last_seen,
                layout_row_id=str(layout.id) if layout else None,
            )
        )

    # ---- Sort: most-docs-first, biggest-gap-first inside a bucket -------
    rows.sort(
        key=lambda r: (
            -r.total_documents,
            _HINT_STATUS_RANK.get(r.hint_status, 99),
            r.bank_name.lower(),
        )
    )
    return rows


def _summary_counts(rows: list[_BankRow]) -> dict[str, int]:
    """Header summary: total banks + per-status counts.

    ``image_only`` is omitted: there is no source column today that
    distinguishes vision-routed parses from text-extracted ones. The
    summary row stays honest until that signal lands.
    """
    return {
        "total_banks": len(rows),
        "manual_hints": sum(1 for r in rows if r.hint_status == "manual"),
        "auto_hints": sum(1 for r in rows if r.hint_status == "auto"),
        "no_hints": sum(1 for r in rows if r.hint_status == "none"),
    }


@router.get("/bank-coverage", response_class=HTMLResponse)
async def bank_coverage_view(
    request: Request,
    layouts: Annotated[BankLayoutRepository, Depends(get_bank_layout_repository)],
    documents: Annotated[DocumentRepository, Depends(get_repository)],
) -> HTMLResponse:
    """Render the bank-coverage dashboard."""
    rows = _build_rows(layouts=layouts, documents=documents)
    summary = _summary_counts(rows)
    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "bank_coverage.html.j2",
            {
                "active": "Admin",
                "rows": rows,
                "summary": summary,
            },
        ),
    )


def _row_partial(
    request: Request,
    *,
    layouts: BankLayoutRepository,
    documents: DocumentRepository,
    bank_name: str,
) -> HTMLResponse:
    """Render the single-row partial for HTMX outerHTML swap.

    Re-runs the full aggregation rather than mutating a single row
    in-place because the per-bank state can change in either direction
    (a bump that pushes a row above the threshold also flips its
    hint_status; we want the rendered status to match the new truth).
    Cheap on the admin path; the aggregation caps at ``_DOCS_SCAN_CAP``.
    """
    rows = _build_rows(layouts=layouts, documents=documents)
    key = bank_name.strip().lower()
    target = next((r for r in rows if r.bank_name_normalized == key), None)
    if target is None:
        # Bank doesn't exist in either source — return an empty row so
        # HTMX still has something to swap. Defensive — should never
        # happen on a normal POST originated from the page.
        return HTMLResponse(content="")
    html = templates.get_template("_bank_coverage_row.html.j2").render(request=request, row=target)
    return HTMLResponse(content=html)


@router.post(
    "/bank-coverage/{bank_name}/bump-parse-count",
    response_class=HTMLResponse,
    dependencies=[Depends(underwriter_or_admin)],
)
async def bank_coverage_bump_parse_count(
    request: Request,
    bank_name: Annotated[str, Path(min_length=1)],
    layouts: Annotated[BankLayoutRepository, Depends(get_bank_layout_repository)],
    documents: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Increment ``successful_parses`` by 1 for this bank (operator-authorized).

    Mirrors the script-side ``_bump_parse_count_one`` posture: we
    re-use the existing repository ``upsert_success`` path to atomically
    bump the counter and refresh ``last_seen``. The fingerprint argument
    is intentionally empty here — the bump is a manual unlock, not a
    real parse, so we have no fingerprint deltas to merge.
    """
    layouts.upsert_success(bank_name=bank_name, fingerprint={})
    row = layouts.find_by_bank_name(bank_name)
    audit.record(
        actor="dashboard",
        action="bank_coverage.parse_count_bumped",
        subject_type="bank_layout",
        subject_id=row.id if row else None,
        # Bank name is PII per CLAUDE.md (logger masks the key); the
        # row UUID + bump_delta answer "did the operator override the
        # parse count for some bank today" without leaking the bank.
        details={
            "bump_delta": 1,
            "new_successful_parses": row.successful_parses if row else 0,
            "note": "operator-authorized backfill via /ui/bank-coverage",
        },
    )
    return _row_partial(request, layouts=layouts, documents=documents, bank_name=bank_name)


@router.post(
    "/bank-coverage/{bank_name}/generate-hints",
    response_class=HTMLResponse,
    dependencies=[Depends(underwriter_or_admin)],
)
async def bank_coverage_generate_hints(
    request: Request,
    bank_name: Annotated[str, Path(min_length=1)],
    layouts: Annotated[BankLayoutRepository, Depends(get_bank_layout_repository)],
    documents: Annotated[DocumentRepository, Depends(get_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
) -> HTMLResponse:
    """Enqueue auto-hint generation for ONE bank.

    Posture mirrors ``enqueue_background_checks``: if the arq pool is
    wired we hand the work off; otherwise we capture into a list on
    app state so tests + dev can observe the request shape. The work
    itself (decrypt page 1 of recent successful parses → Bedrock →
    write hints) lives in the auto-hints agent's path; this route
    writes the operator intent.
    """
    pool: Any | None = getattr(request.app.state, "arq_pool", None)
    enqueued = False
    try:
        if pool is not None:
            await pool.enqueue_job("run_generate_hints_for_bank", bank_name)
        else:
            pending = getattr(request.app.state, "pending_generate_hints_jobs", None)
            if pending is None:
                pending = []
                request.app.state.pending_generate_hints_jobs = pending
            pending.append({"bank_name": bank_name})
        enqueued = True
    except Exception as exc:  # pragma: no cover — runtime Redis blip only
        audit.record(
            actor="dashboard",
            action="bank_coverage.generate_hints_enqueue_failed",
            subject_type="bank_layout",
            details={
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:200],
            },
        )

    if enqueued:
        existing = layouts.find_by_bank_name(bank_name)
        audit.record(
            actor="dashboard",
            action="bank_coverage.generate_hints_enqueued",
            subject_type="bank_layout",
            subject_id=existing.id if existing else None,
            details={
                "note": (
                    "consumed by the auto-hints generator job when it "
                    "lands; until then the operator runs the script "
                    "manually with --bank-name."
                ),
            },
        )

    return _row_partial(request, layouts=layouts, documents=documents, bank_name=bank_name)


__all__ = ["router"]
