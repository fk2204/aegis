"""Bank-layout learning sub-router — operator-curated extraction hints.

Routes:
  * ``GET  /ui/bank-layouts``                       — list all banks
  * ``POST /ui/bank-layouts/{bank_name}/hints``     — set/clear hints

The list view renders every bank the parser has seen (or the operator
has primed) with its successful-parse count, last-seen timestamp, and
an editable hints textarea. The POST handler returns the rendered row
partial for an HTMX outerHTML swap so the page does not full-reload on
save.

URL path note: bank names contain spaces, slashes, and other operator-
typed characters; the path parameter is unconstrained and consumed
URL-decoded by FastAPI. Repository lookup is case-insensitive (see
``BankLayoutRepository.set_hints``) so a slightly off URL still resolves
to the right row.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Path, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_audit, get_bank_layout_repository
from aegis.audit import AuditLog
from aegis.bank_layouts import BankLayoutRepository, BankLayoutRow
from aegis.ops.operators import resolve_operator_email
from aegis.web._templates import templates

router = APIRouter()


@router.get("/bank-layouts", response_class=HTMLResponse)
async def bank_layouts_list(
    request: Request,
    layouts: Annotated[BankLayoutRepository, Depends(get_bank_layout_repository)],
) -> HTMLResponse:
    """Render the full bank-layout list.

    Rows sorted newest-seen first (NULLS LAST handled by the
    repository) so the banks the parser is actively learning from
    appear at the top of the operator's scan.
    """
    rows = layouts.list_all()
    return templates.TemplateResponse(
        request,
        "bank_layouts.html.j2",
        {"rows": rows},
    )


@router.post("/bank-layouts/{bank_name}/hints", response_model=None)
async def bank_layouts_set_hints(
    bank_name: Annotated[str, Path(min_length=1)],
    layouts: Annotated[BankLayoutRepository, Depends(get_bank_layout_repository)],
    audit: Annotated[AuditLog, Depends(get_audit)],
    hints: Annotated[str, Form()] = "",
    actor_email: Annotated[str | None, Depends(resolve_operator_email)] = None,
) -> HTMLResponse:
    """Set or clear operator-authored extraction hints for one bank.

    Creates a primed row (``successful_parses=0``) when the bank has
    never been parsed. Empty / whitespace-only ``hints`` clears the
    column (see ``BankLayoutRepository.set_hints``).

    Audit details deliberately avoid PII: ``bank_name`` is in the
    logger's PII-key set (CLAUDE.md), so we do NOT include it as a
    top-level key in ``details`` — the masking layer would replace it
    with ``***`` anyway and downstream regulator queries would be
    answerless. The audit ``subject_id`` already references the row
    UUID (which the operator can join back to the bank_layouts table
    when they need the bank's name); ``hints_chars`` answers the
    "did the operator change the hints last week?" question without
    leaking the prompt content.
    """
    row = layouts.set_hints(bank_name=bank_name, hints=hints)
    audit.record(
        actor="dashboard",
        actor_email=actor_email,
        action="bank_layouts.hints_updated",
        subject_type="bank_layout",
        subject_id=row.id,
        details={
            "hints_chars": len(row.extraction_hints or ""),
        },
    )
    return _render_bank_layout_row(row)


def _render_bank_layout_row(row: BankLayoutRow) -> HTMLResponse:
    """Render the single-row partial for HTMX outerHTML swap.

    Mirrors the merchants router's ``_render_notes_block`` shape so the
    template singleton is used end-to-end (no per-route template
    bootstrap).
    """
    html = templates.get_template("_bank_layout_row.html.j2").render(row=row)
    return HTMLResponse(content=html)


__all__ = ["router"]
