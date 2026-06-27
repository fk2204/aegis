"""Operator-triggered bank reparse — POST ``/ui/bank-coverage/{bank_name}/reparse-manual-review``.

Sibling route to Phase 1 Agent 4's ``bank_coverage`` router. Kept in a
separate module so this commit doesn't depend on the merge order of
the Phase 1 Push 4 commit — the router lands additively. After both
land, the parent reviewer can fold this single route into the main
``bank_coverage`` router file if they want a single-file surface for
the page.

Same fire-and-forget enqueue pipeline the
``reparse_bank_manual_review`` arq job runs, called inline from the
HTMX handler so the response carries an accurate enqueue count to
render in the swap target. Role-gated to Admin OR Underwriter (viewer
gets 403) — the operation re-runs Bedrock extractions, which has
non-trivial cost.

Two audit-row pairs land per request (per CLAUDE.md auditability —
every operator action leaves a row even on no-op outcomes):

  * ``bank_layouts.reparse_operator_triggered`` — written FIRST so the
    operator's intent is durable even if the enqueue helper raises.
    Subject is ``bank_layout``; details carry the bank_name, the
    actor email (PII-safe: this is operator email, not merchant data).
  * The helper's standard ``bank_layouts.reparse_enqueued`` (one per
    doc) + ``bank_layouts.reparse_batch_complete`` (one summary).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import (
    get_audit,
    get_pdf_store_repository,
)
from aegis.audit import AuditLog
from aegis.bank_layouts.reparse import enqueue_bank_reparse
from aegis.logger import get_logger
from aegis.ops.operators import Operator
from aegis.pdf_store import PdfStoreRepository
from aegis.web._role_gate import underwriter_or_admin

_log = get_logger(__name__)

router = APIRouter()


def _get_arq_pool(request: Request) -> object | None:
    """Return the FastAPI-app-state arq pool when configured, else None.

    Production wiring lives in ``aegis.api.app::create_app``; the pool
    is attached as ``app.state.arq_pool`` at startup. Tests typically
    leave it unset, in which case ``enqueue_bank_reparse`` no-ops with
    a logged warning rather than crashing the endpoint.
    """
    return getattr(request.app.state, "arq_pool", None)


@router.post(
    "/bank-coverage/{bank_name}/reparse-manual-review",
    response_class=HTMLResponse,
)
async def reparse_bank_manual_review_endpoint(
    request: Request,
    bank_name: str,
    audit: Annotated[AuditLog, Depends(get_audit)],
    pdf_store: Annotated[PdfStoreRepository, Depends(get_pdf_store_repository)],
    actor: Annotated[Operator, Depends(underwriter_or_admin)],
) -> HTMLResponse:
    """Re-enqueue every sealed manual_review doc for ``bank_name``.

    Returns a small HTML fragment HTMX swaps into the row's action
    cell — `"Reparse enqueued: N docs"` (or `"No candidates"` when the
    bank has no sealed manual_review docs).
    """
    # Audit the operator's intent BEFORE the helper runs so the action
    # is durable even if the enqueue helper raises mid-batch.
    audit.record(
        action="bank_layouts.reparse_operator_triggered",
        actor=f"operator:{actor.email}",
        actor_email=actor.email,
        subject_type="bank_layout",
        details={"bank_layout_name": bank_name},
    )
    pool = _get_arq_pool(request)
    enqueued = await enqueue_bank_reparse(
        bank_name=bank_name,
        pool=pool,
        audit=audit,
        pdf_store=pdf_store,
        trigger="operator",
    )
    if enqueued == 0:
        body = (
            f'<span data-test-id="reparse-result" '
            f'data-bank="{bank_name}" data-enqueued="0">'
            f"No manual_review candidates for {bank_name}"
            f"</span>"
        )
    else:
        body = (
            f'<span data-test-id="reparse-result" '
            f'data-bank="{bank_name}" data-enqueued="{enqueued}">'
            f"Reparse enqueued: {enqueued} doc"
            f"{'s' if enqueued != 1 else ''}"
            f"</span>"
        )
    return HTMLResponse(content=body)


__all__ = ["router"]
