"""Shared module-level helpers for the operator dashboard sub-routers.

Extracted from ``router.py`` during R4.1 so multiple sub-routers can
reference these without re-importing the 5k-line aggregator. Anything
that lives here is consumed by routes in MULTIPLE domain sub-routers
(or by a sub-router AND something still inside router.py).
"""

from __future__ import annotations

_AGGREGATE_LABELS: dict[str, str] = {
    "true_revenue": "True Revenue",
    "avg_daily_balance": "Average Daily Balance",
    "num_nsf": "NSF Count",
    "days_negative": "Days Negative",
    "mca_daily_total": "MCA Daily Total",
}

# Per-aggregate unit hint shown under the KPI value (e.g. "$" amount,
# "days", "count"). Kept aligned with _AGGREGATE_LABELS — every key
# present in labels must have an entry here so the KPI tile can format.
_AGGREGATE_UNIT_KIND: dict[str, str] = {
    "true_revenue": "money",
    "avg_daily_balance": "money",
    "num_nsf": "count",
    "days_negative": "days",
    "mca_daily_total": "money",
}

_AGGREGATE_SOURCE_FIELDS: dict[str, str] = {
    "true_revenue": "true_revenue_source_ids",
    "avg_daily_balance": "avg_daily_balance_source_ids",
    "num_nsf": "num_nsf_source_ids",
    "days_negative": "days_negative_source_ids",
    "mca_daily_total": "mca_daily_total_source_ids",
}


__all__ = [
    "_AGGREGATE_LABELS",
    "_AGGREGATE_SOURCE_FIELDS",
    "_AGGREGATE_UNIT_KIND",
]
