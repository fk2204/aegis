"""Merchant identity + persistence.

``MerchantRow`` is the application view of the ``merchants`` table.
``MerchantRepository`` is the persistence Protocol used by API routes
and the scorer's ``build_score_input`` (Phase 5+).
"""

from __future__ import annotations

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    MerchantNotFoundError,
    MerchantRepository,
    SupabaseMerchantRepository,
)

__all__ = [
    "InMemoryMerchantRepository",
    "MerchantNotFoundError",
    "MerchantRepository",
    "MerchantRow",
    "SupabaseMerchantRepository",
]
