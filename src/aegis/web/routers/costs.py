"""Bedrock per-call cost dashboard (migration 078 surface).

Two reads:

* ``GET /ui/costs`` — month picker + per-merchant breakdown + per-document
  breakdown for the selected month + 6-month trend.

The route is operator-only (lives under ``/ui/*`` which sits behind
Cloudflare Access). TODO once Agent 4's role gate lands on main: wrap
with ``require_role("admin")``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from aegis.api.deps import get_llm_cost_repository, get_merchant_repository
from aegis.logger import get_logger
from aegis.merchants import MerchantRepository
from aegis.merchants.repository import MerchantNotFoundError
from aegis.ops.llm_cost_repository import LLMCostRepository
from aegis.web._templates import templates

router = APIRouter()

_log = get_logger(__name__)


def _month_bounds(month_iso: str) -> tuple[datetime, datetime]:
    """Return (start, end) UTC datetimes for an ISO ``YYYY-MM`` string."""
    year_str, month_str = month_iso.split("-")
    year = int(year_str)
    month = int(month_str)
    start = datetime(year, month, 1, tzinfo=UTC)
    # Next month start, clamped to year roll-over.
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return start, end


def _current_month_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


@router.get("/costs", response_class=HTMLResponse)
async def costs_view(
    request: Request,
    repo: Annotated[LLMCostRepository, Depends(get_llm_cost_repository)],
    merchants: Annotated[MerchantRepository, Depends(get_merchant_repository)],
    month: Annotated[
        str | None,
        Query(description="YYYY-MM; defaults to current UTC month"),
    ] = None,
) -> HTMLResponse:
    """Render the Bedrock cost dashboard.

    Numbers in the UI display to 2 decimal USD; the underlying column
    is numeric(10,6) so we retain sub-cent precision in the repo and
    quantize only at render time.
    """
    selected_month = (month or _current_month_iso()).strip()
    start, end = _month_bounds(selected_month)

    in_window = repo.list_in_window(start=start, end=end)
    per_merchant_rows = repo.per_merchant(start=start, end=end)
    per_document_rows = repo.per_document(start=start, end=end)
    monthly_trend = repo.monthly_trend(months=6)

    # Resolve merchant names; merchant_id is opaque so the UI needs the
    # human label. PII rule: merchant business names are NOT logged —
    # they're surfaced only in the rendered HTML to the SSO operator.
    merchant_name_by_id: dict[str, str] = {}
    for row in per_merchant_rows:
        if row.merchant_id is None:
            continue
        try:
            merchant = merchants.get(row.merchant_id)
        except MerchantNotFoundError:
            # Merchant may have been soft-deleted since the cost row landed;
            # show the bare merchant_id in the table instead of a name.
            continue
        except Exception as exc:
            _log.warning(
                "costs_view.merchant_lookup_failed merchant_id=%s error=%s",
                row.merchant_id,
                exc,
            )
            continue
        name = (merchant.business_name or merchant.dba or "").strip()
        if name:
            merchant_name_by_id[str(row.merchant_id)] = name

    total_cost = sum((r.estimated_cost_usd for r in in_window), Decimal("0")).quantize(
        Decimal("0.01")
    )
    total_input_tokens = sum(r.input_tokens for r in in_window)
    total_output_tokens = sum(r.output_tokens for r in in_window)

    return cast(
        "HTMLResponse",
        templates.TemplateResponse(
            request,
            "costs.html.j2",
            {
                "active": "Admin",
                "selected_month": selected_month,
                "total_cost_usd_display": f"{total_cost:.2f}",
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "call_count": len(in_window),
                "per_merchant_rows": per_merchant_rows,
                "per_document_rows": per_document_rows,
                "monthly_trend": monthly_trend,
                "merchant_name_by_id": merchant_name_by_id,
            },
        ),
    )
