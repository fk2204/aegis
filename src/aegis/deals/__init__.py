"""Deal entity — a derived view, not a stored table.

A "deal" in AEGIS is the pairing of a merchant with a specific parsed
bank-statement document. There is no ``deals`` table on Postgres. The
identifier is computed:

    deal_id = f"{merchant_id}:{document_id}"

— a deterministic composite string. See ``aegis.deals.models`` for the
rationale (parse_deal_id round-trip).

The phase-7 audit finding F1 locks this shape: "deal is a (merchant_id,
document_id) derived view, no new table." This package exists so the API,
the dashboard, and the submissions table all have a single typed
projection (``DealRow``) and a single repository contract (``DealRepository``)
rather than re-deriving the join in five places.
"""

from aegis.deals.models import DealRow, format_deal_id, parse_deal_id
from aegis.deals.repository import (
    DealNotFoundError,
    DealRepository,
    InMemoryDealRepository,
    SupabaseDealRepository,
)

__all__ = [
    "DealNotFoundError",
    "DealRepository",
    "DealRow",
    "InMemoryDealRepository",
    "SupabaseDealRepository",
    "format_deal_id",
    "parse_deal_id",
]
