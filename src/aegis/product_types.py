"""Product-type literal + default for Commera's 6 lending products.

This module is intentionally tiny — it's a single ``Literal`` plus a
default constant plus a label map. Phase A Agent 7 (the product-type
data model + migration 080) lands the schema-side counterpart
(``product_type`` columns on ``merchants`` / ``decisions`` /
``funder_note_submissions``). Phase A Agent 8 (this commit, scoring)
and Agent 9 (narrator + funder matching) both import from here so the
literal is single-sourced.

If A7's commit lands first and creates this module too, the merge
should keep whichever version has the wider product set (they should
match — both are derived from the same operator spec).

The 6 products:

* ``revenue_based`` — MCA / RBF. Existing AEGIS default. Offer sized
  off monthly true revenue x multiple x holdback capacity.
* ``business_loan`` — fixed-term amortising loan with APR + monthly
  payment. Offer sized off monthly revenue with a placeholder rate.
* ``line_of_credit`` — revolving facility with a credit limit + per-
  draw rate. Offer sized off monthly revenue.
* ``equipment`` — equipment finance against operator-supplied
  equipment cost. Offer sized off ``equipment_cost`` kwarg.
* ``asset_based`` — A/R + inventory revolver. Offer sized off
  operator-supplied collateral with a placeholder advance rate.
* ``receivables`` — invoice factoring. Offer sized off advance rate
  + reserve + factoring fee defaults.
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
"""Closed set of 6 lending products Commera offers."""

DEFAULT_PRODUCT_TYPE: Final[ProductType] = "revenue_based"
"""Default for any merchant whose ``product_type`` field is missing /
null. Mirrors the migration-080 column default so the in-code default
and the schema default never drift."""

PRODUCT_TYPE_LABELS: Final[dict[ProductType, str]] = {
    "revenue_based": "Revenue-based financing",
    "business_loan": "Business term loan",
    "line_of_credit": "Line of credit",
    "equipment": "Equipment finance",
    "asset_based": "Asset-based lending",
    "receivables": "Invoice factoring",
}
"""Operator-facing labels for each product type. Used by templates +
the dossier product chip."""

__all__ = [
    "DEFAULT_PRODUCT_TYPE",
    "PRODUCT_TYPE_LABELS",
    "ProductType",
]
