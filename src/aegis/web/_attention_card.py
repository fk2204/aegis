"""Data shapes for the Today / Review Queue attention cards.

Chunk A of the Proposal 4 redesign: defines the structured
``AttentionCard`` (merchant context + categorized flags + doc list) so
Today and Review Queue can share one card vocabulary. The existing
templates still render the legacy flat-chip layout in chunk A —
the dataclass fields cover both the old shape (``merchant_label``,
``doc_count``, ``worst_fraud_score``, ``unique_flags``, ``documents``)
and the new shape (``merchant_state``, ``merchant_naics``,
``requested_amount``, ``fraud_band``, ``tier``, ``flags``). Chunks B
and C migrate the templates to consume ``flags`` (``CategorizedFlags``)
directly, after which ``unique_flags`` becomes redundant.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from aegis.web._flag_labels import CategoryName, HumanFlag, humanize_flag

# Display order for category groups on the card. ``stacking`` and
# ``fabrication`` come first because they're the deal-defining flag
# families; soft + composite signals last because they're informational
# rather than decision-driving.
_CATEGORY_DISPLAY_ORDER: Final[tuple[CategoryName, ...]] = (
    "stacking",
    "fabrication",
    "stress",
    "concentration",
    "hidden_account",
    "recency",
    "tampering",
    "soft",
    "math",
    "composite",
    "unknown",
)

# Operator-facing labels for each category group heading. Chunk B's
# template consumes these directly so the section labels stay editable
# in one place rather than scattered across Jinja.
CATEGORY_LABELS: Final[dict[str, str]] = {
    "stacking":       "Stacking & funder position",
    "fabrication":    "Revenue fabrication",
    "stress":         "Cashflow stress",
    "concentration":  "Customer / processor concentration",
    "hidden_account": "Hidden-account signal",
    "recency":        "Account recency",
    "tampering":      "PDF tampering",
    "soft":           "Context signals",
    "math":           "Validation gate",
    "composite":      "Composite signals",
    "unknown":        "Unknown",
}


@dataclass(frozen=True)
class CategorizedFlags:
    """Flags split into a decline-class bucket and per-category buckets.

    Decline-class flags (severity_band == ``decline``) are pulled to the
    top of the card regardless of category — workers should not have to
    scan a list to find the dealbreakers. Every other flag lands in its
    category bucket, preserving first-seen order within the category.

    Empty category buckets are omitted so the template renders nothing
    for empty groups instead of an empty heading.
    """

    decline_class: list[HumanFlag]
    by_category: dict[str, list[HumanFlag]]

    @property
    def total_count(self) -> int:
        return len(self.decline_class) + sum(
            len(v) for v in self.by_category.values()
        )

    @property
    def is_empty(self) -> bool:
        return self.total_count == 0


@dataclass(frozen=True)
class AttentionCard:
    """One merchant's worth of context for Today / Review Queue cards.

    Carries merchant identity + key facts at the top, categorized flags
    in the middle, and the per-document list at the bottom. Chunks B
    and C rewrite the Today and Review Queue templates to consume this
    shape directly.

    ``tier`` is the deal tier (A/B/C/D/F) from running ``score_deal``
    on the merchant's analyzed documents. It falls back to ``None``
    when the merchant has no analyzable documents, when OFAC is stale,
    or on any scoring exception — the redesign header degrades to "no
    tier" rather than crashing the queue.
    """

    merchant_id: str | None
    merchant_label: str
    merchant_state: str | None
    merchant_naics: str | None
    requested_amount: Decimal | None
    worst_fraud_score: int | None
    fraud_band: str  # "clear" | "review" | "decline" | "unknown"
    tier: str | None
    doc_count: int
    documents: list[dict[str, Any]]
    flags: CategorizedFlags


def categorize_flags(raw_flags: list[str]) -> CategorizedFlags:
    """Categorize and deduplicate a list of raw flag strings.

    Each raw flag is humanized via ``humanize_flag``. Duplicates (by
    flag code) are dropped on first-seen order. Flags whose severity
    band is ``decline`` go into ``decline_class``; everything else into
    ``by_category`` under the flag's category in fixed display order.
    Empty category buckets are omitted from the result.
    """
    seen: set[str] = set()
    decline_class: list[HumanFlag] = []
    raw_buckets: dict[str, list[HumanFlag]] = {}

    for raw in raw_flags:
        hf = humanize_flag(raw)
        if hf.code in seen:
            continue
        seen.add(hf.code)
        if hf.severity_band == "decline":
            decline_class.append(hf)
            continue
        raw_buckets.setdefault(hf.category, []).append(hf)

    # Apply display-order to the by_category dict so the template can
    # iterate the dict and get the right group order for free.
    ordered: dict[str, list[HumanFlag]] = {}
    for known_cat in _CATEGORY_DISPLAY_ORDER:
        if known_cat in raw_buckets:
            ordered[known_cat] = raw_buckets[known_cat]
    # Defensive: any category outside the declared order (humanize_flag
    # always returns a known CategoryName, but a typo in a future code
    # path shouldn't drop flags silently) goes at the end.
    for extra_cat, extra_flags in raw_buckets.items():
        if extra_cat not in ordered:
            ordered[extra_cat] = extra_flags

    return CategorizedFlags(decline_class=decline_class, by_category=ordered)


def derive_fraud_band(score: int | None) -> str:
    """Map a fraud_score to its operator-readable band.

    Mirrors the thresholds used by ``router._fraud_band`` (the Jinja
    filter) so the card-level band and any in-template band rendering
    agree. ``None`` resolves to ``unknown`` rather than ``clear`` so an
    unscored merchant doesn't masquerade as a green deal.
    """
    if score is None:
        return "unknown"
    if score < 35:
        return "clear"
    if score < 65:
        return "review"
    return "decline"


__all__ = [
    "CATEGORY_LABELS",
    "AttentionCard",
    "CategorizedFlags",
    "categorize_flags",
    "derive_fraud_band",
]
