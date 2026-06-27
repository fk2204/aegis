"""Canonical product-type literal for Commera's 6 supported products.

PA A7 owns the merchant-side wiring (``MerchantRow.product_type``,
migration 080, Close field-map read). This module is the single
source of truth for the literal + display labels so consumers across
``scoring``, ``scoring_v2``, ``funders``, ``web`` import from one
place and the spelling can never drift.

If PA A7 lands first the merge resolution is trivial — the literal
values here are the spec; consumers should import from here.
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

# Display labels for operator-facing UI. Keys are exhaustive over
# ``ProductType``; ``mypy --strict`` plus the test
# ``test_product_type_labels_cover_every_literal`` enforce that.
PRODUCT_TYPE_LABELS: Final[dict[str, str]] = {
    "revenue_based": "Revenue-based",
    "business_loan": "Business loan",
    "line_of_credit": "Line of credit",
    "equipment": "Equipment finance",
    "asset_based": "Asset-based",
    "receivables": "Receivables factoring",
}


def coerce_product_type(value: str | None) -> ProductType:
    """Return ``value`` if it is a known product type, else fall back.

    Used at consumer sites that read ``merchant.product_type`` defensively
    via ``getattr`` until PA A7 wires the field through. Unknown / None
    values land on ``"revenue_based"`` — Commera's default product and
    the historical assumption for every existing dossier.
    """
    if value in PRODUCT_TYPE_LABELS:
        return value  # type: ignore[return-value]  # narrowed by dict membership
    return "revenue_based"


__all__ = [
    "PRODUCT_TYPE_LABELS",
    "ProductType",
    "coerce_product_type",
]
