"""Commera lending product taxonomy.

Single source of truth for the six lending products AEGIS supports as of
migration 080. The ``ProductType`` literal is consumed by
``MerchantRow``, ``DecisionPayload``, ``FunderNoteSubmissionRow``, the
Close webhook field-map, the offer-sizing engine (Phase A Agent 8),
narrator framing (Phase A Agent 9), and funder matching (Phase A
Agent 9).

Per AEGIS operating-principle 4 (no fabricated defaults), the project
default ``revenue_based`` reflects pre-migration-080 reality: Commera
was a pure revenue-based-financing (MCA) broker. Every legacy merchant
row carries that value via the migration-080 ``DEFAULT``.
"""

from __future__ import annotations

from typing import Final, Literal

ProductType = Literal[
    "revenue_based",
    "business_loan",
    "line_of_credit",
    "equipment",
    "asset_based",
    "receivables",
]

DEFAULT_PRODUCT_TYPE: Final[ProductType] = "revenue_based"

# Operator-facing labels for dashboards, narrator framing, and CSV
# exports. Keep these stable — changing the label changes UI copy.
PRODUCT_TYPE_LABELS: Final[dict[ProductType, str]] = {
    "revenue_based": "Revenue-Based Financing",
    "business_loan": "Business Term Loan",
    "line_of_credit": "Line of Credit",
    "equipment": "Equipment Financing",
    "asset_based": "Asset-Based Lending",
    "receivables": "Receivables Factoring",
}

# Tuple of all valid product_type values. Useful for tests and for the
# ``isinstance`` check below.
PRODUCT_TYPE_VALUES: Final[tuple[ProductType, ...]] = (
    "revenue_based",
    "business_loan",
    "line_of_credit",
    "equipment",
    "asset_based",
    "receivables",
)


def is_valid_product_type(value: object) -> bool:
    """Return True iff ``value`` is one of the six known product types."""
    return isinstance(value, str) and value in PRODUCT_TYPE_VALUES


def coerce_product_type(value: object) -> ProductType:
    """Coerce an arbitrary value to a valid ``ProductType``.

    Returns the value verbatim when valid; otherwise returns
    ``DEFAULT_PRODUCT_TYPE`` ("revenue_based"). Used at boundaries
    where the input may be ``None`` (legacy MerchantRow without
    product_type), a stale Close field value, or a typo from operator
    UI — the default keeps the matcher / narrator working without
    needing each call site to handle the None branch.
    """
    if isinstance(value, str) and value in PRODUCT_TYPE_VALUES:
        return value
    return DEFAULT_PRODUCT_TYPE


__all__ = [
    "DEFAULT_PRODUCT_TYPE",
    "PRODUCT_TYPE_LABELS",
    "PRODUCT_TYPE_VALUES",
    "ProductType",
    "coerce_product_type",
    "is_valid_product_type",
]
