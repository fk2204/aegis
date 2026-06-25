"""Shadow-signal review listing — operator-facing surface for the weekly cron.

Mirrors the data the ``aegis.workers.run_shadow_review_cron`` Wednesday
pass aggregates: every document parsed in the trailing 7 days with at
least one ``[SHADOW] *`` flag, grouped by document and listing the
contributing flag codes.

Read-only. The page exists so the operator can drill from the Today
dashboard "Shadow signals this week" attention card into the actual
document list — and so the corpus-validation review for promoting a
shadow detector to live has a single weekly URL to point at.

Distinct from ``/ui/shadow-signals`` (cross-merchant
``merchants_shadow_signals`` table — different surface, different
detectors).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_merchant_repository, get_repository
from aegis.merchants.repository import MerchantRepository
from aegis.ops.shadow_review import (
    DEFAULT_WINDOW_DAYS,
    build_shadow_review_attention_section,
)
from aegis.storage import DocumentRepository
from aegis.web._templates import templates

router = APIRouter()


@router.get("/shadow-review", response_class=HTMLResponse)
async def shadow_review_view(
    request: Request,
    docs: Annotated[DocumentRepository, Depends(get_repository)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
) -> HTMLResponse:
    """List documents with ``[SHADOW] *`` fires in the last 7 days.

    Uses ``build_shadow_review_attention_section`` with an unbounded
    ``max_cards`` so every contributing document is shown (the Today
    card caps at 5; this page is the "see all" expansion).
    """
    count, source_document_ids, cards = build_shadow_review_attention_section(
        docs=docs,
        merchants=merchants,
        max_cards=10_000,  # effectively unbounded — the page is the full list
    )
    return templates.TemplateResponse(
        request,
        "shadow_review.html.j2",
        {
            "window_days": DEFAULT_WINDOW_DAYS,
            "doc_count": count,
            "source_document_ids": source_document_ids,
            "cards": cards,
        },
    )
