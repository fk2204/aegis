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

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from aegis.web._flag_labels import (
    CategoryName,
    FlagSourceTransaction,
    HumanFlag,
    humanize_flag,
)

if TYPE_CHECKING:
    from aegis.parser.models import ClassifiedTransaction
    from aegis.storage import AnalysisRow

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
    # Migration 034 — merchant lifecycle status surfaced on the card so
    # the Today / Review attention queues can render a status chip
    # ("provisional" / "needs naming") next to non-finalized merchants.
    # ``None`` for unlinked groups (the "—" bucket) and as a safe
    # default for legacy call sites that don't thread the value yet.
    merchant_status: str | None = None


@dataclass(frozen=True)
class ReviewQueueCard:
    """One card per document in the manual_review queue.

    Where ``AttentionCard`` aggregates flags across a merchant's docs
    (the Today triage view), ``ReviewQueueCard`` is per-document — each
    card is one task the operator works through. Reuses the same
    merchant header context + categorized flags vocabulary as
    ``AttentionCard`` so the two surfaces share one visual language.

    ``tier`` is the deal-level tier from running ``score_deal`` on the
    merchant's full document set. It's identical across every
    ReviewQueueCard belonging to a single merchant — the chunk-C
    builder caches the value per merchant so multiple docs from one
    merchant only pay the scoring cost once.

    Per-doc fields:
      * ``document_id`` — UUID string, used for the "open document" link
      * ``filename`` — original PDF filename, surfaced so the operator
        can match against their own naming
      * ``uploaded_at`` — pre-formatted ``YYYY-MM-DD HH:MM``
      * ``fraud_score`` / ``fraud_band`` — this document's score, not
        the merchant's aggregate
    """

    document_id: str
    filename: str
    uploaded_at: str
    fraud_score: int | None
    fraud_band: str

    merchant_id: str | None
    merchant_label: str
    merchant_state: str | None
    merchant_naics: str | None
    requested_amount: Decimal | None
    tier: str | None

    flags: CategorizedFlags
    # Migration 034 — same as AttentionCard.merchant_status. Surfaced
    # on the per-document Review Queue card so the worker can tell at
    # a glance whether the linked merchant is provisional or awaiting
    # manual naming. Defaults to ``None`` for backward compat with any
    # builder that doesn't yet thread the field.
    merchant_status: str | None = None


@dataclass(frozen=True)
class DocumentPatternContext:
    """One document's worth of context for ``PatternIndex.build_for_merchant``.

    Carries the doc's identity (``document_id`` + ``filename``) and the
    persisted PatternAnalysis cache + the document's classified
    transactions. ``analysis`` is None for docs whose AnalysisRow is
    missing (orphaned parse_status=manual_review with no analyses row)
    OR present but with ``pattern_analysis=None`` (legacy rows parsed
    before stage 2 chunk 2 deployed). Either case contributes no
    drill-down entries to the index — the chips for that doc's flags
    degrade to plain spans.
    """

    document_id: UUID
    filename: str
    analysis: AnalysisRow | None
    transactions: list[ClassifiedTransaction]


@dataclass(frozen=True)
class PatternIndex:
    """Lookup table from flag code → contributing FlagSourceTransactions.

    Built per attention/review card. Reads from the doc's persisted
    ``AnalysisRow.pattern_analysis`` (migration 032, populated by stage
    2 chunk 2) and resolves each Pattern's ``source_ids`` to the actual
    ClassifiedTransaction rows, wrapping each as a
    ``FlagSourceTransaction`` tagged with the contributing doc's
    filename.

    Two builders:

    * ``build_for_document`` — per-doc form for the Review Queue, where
      each card is one document. Every row in the resulting index
      carries the same filename.
    * ``build_for_merchant`` — per-merchant form for the Today
      attention queue, where each card aggregates flags across the
      merchant's docs. Cross-doc filename tagging lets workers scan
      which upload contributed each row.

    An empty index (no pattern_analysis available, or no patterns
    inside) is the graceful-degradation contract: ``categorize_flags``
    proceeds normally and the chips render as plain spans without
    drill-down. Same shape as passing ``pattern_index=None``.
    """

    by_code: dict[str, list[FlagSourceTransaction]]

    def get(self, code: str) -> list[FlagSourceTransaction] | None:
        sources = self.by_code.get(code)
        return sources if sources else None

    @classmethod
    def empty(cls) -> PatternIndex:
        return cls(by_code={})

    @classmethod
    def build_for_document(
        cls,
        *,
        analysis: AnalysisRow | None,
        transactions: list[ClassifiedTransaction],
        document_id: UUID,
        filename: str,
    ) -> PatternIndex:
        """Build a per-document index. Empty when no PatternAnalysis cache."""
        if analysis is None or analysis.pattern_analysis is None:
            return cls.empty()
        by_id: dict[UUID, ClassifiedTransaction] = {t.id: t for t in transactions}
        by_code: dict[str, list[FlagSourceTransaction]] = {}
        seen_per_code: dict[str, set[UUID]] = {}
        for p in analysis.pattern_analysis.patterns:
            for source_id in p.source_ids:
                tx = by_id.get(source_id)
                if tx is None:
                    continue
                if source_id in seen_per_code.setdefault(p.code, set()):
                    continue
                seen_per_code[p.code].add(source_id)
                by_code.setdefault(p.code, []).append(
                    _to_flag_source(
                        tx, document_id=str(document_id), filename=filename
                    )
                )
        return cls(by_code=by_code)

    @classmethod
    def build_for_merchant(
        cls, contexts: list[DocumentPatternContext]
    ) -> PatternIndex:
        """Build a per-merchant index across N docs with filename tagging.

        Within a single (doc, transaction_id) tuple, duplicates are
        dropped per code so two Pattern entries with the same code in
        one doc's analysis don't double-list the same source row.
        Cross-doc duplicates (different doc but same code + tx_id) are
        preserved — the filename column distinguishes them, and the
        operator wants to see each contributing upload.
        """
        by_code: dict[str, list[FlagSourceTransaction]] = {}
        seen_per_code: dict[str, set[tuple[UUID, UUID]]] = {}
        for ctx in contexts:
            if ctx.analysis is None or ctx.analysis.pattern_analysis is None:
                continue
            by_id: dict[UUID, ClassifiedTransaction] = {
                t.id: t for t in ctx.transactions
            }
            for p in ctx.analysis.pattern_analysis.patterns:
                for source_id in p.source_ids:
                    tx = by_id.get(source_id)
                    if tx is None:
                        continue
                    key = (ctx.document_id, source_id)
                    if key in seen_per_code.setdefault(p.code, set()):
                        continue
                    seen_per_code[p.code].add(key)
                    by_code.setdefault(p.code, []).append(
                        _to_flag_source(
                            tx,
                            document_id=str(ctx.document_id),
                            filename=ctx.filename,
                        )
                    )
        return cls(by_code=by_code)


def _to_flag_source(
    tx: ClassifiedTransaction, *, document_id: str, filename: str
) -> FlagSourceTransaction:
    """Adapt a ClassifiedTransaction into the chip-drilldown DTO."""
    return FlagSourceTransaction(
        posted_date=tx.posted_date,
        description=tx.description,
        amount=tx.amount,
        source_page=tx.source_page,
        source_line=tx.source_line,
        document_id=document_id,
        filename=filename,
    )


def categorize_flags(
    raw_flags: list[str],
    pattern_index: PatternIndex | None = None,
) -> CategorizedFlags:
    """Categorize and deduplicate a list of raw flag strings.

    Each raw flag is humanized via ``humanize_flag``. Duplicates (by
    flag code) are dropped on first-seen order. Flags whose severity
    band is ``decline`` go into ``decline_class``; everything else into
    ``by_category`` under the flag's category in fixed display order.
    Empty category buckets are omitted from the result.

    ``pattern_index`` is the chunk-3 drill-down hook. When provided,
    every HumanFlag whose code matches an entry in the index is
    decorated with the contributing ``FlagSourceTransaction`` list,
    which ``_chip_drilldown.html.j2`` renders as an inline ``<details>``
    evidence table. ``None`` (the legacy default) means no drill-down —
    chips render as plain spans. The index itself degrades gracefully:
    a code with no matching entry leaves the chip undecorated.
    """
    seen: set[str] = set()
    decline_class: list[HumanFlag] = []
    raw_buckets: dict[str, list[HumanFlag]] = {}

    for raw in raw_flags:
        hf = humanize_flag(raw)
        if hf.code in seen:
            continue
        seen.add(hf.code)
        hf = _maybe_attach_sources(hf, pattern_index)
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


def _maybe_attach_sources(
    hf: HumanFlag, pattern_index: PatternIndex | None
) -> HumanFlag:
    """Return ``hf`` decorated with ``source_transactions`` if the index
    has entries for its code; otherwise return ``hf`` unchanged.

    The PatternIndex.get contract returns ``None`` for "no entries" so
    we don't accidentally attach an empty list (which the template
    would treat as "drill-down available with zero rows" — confusing).
    """
    if pattern_index is None:
        return hf
    sources = pattern_index.get(hf.code)
    if not sources:
        return hf
    return replace(hf, source_transactions=sources)


__all__ = [
    "CATEGORY_LABELS",
    "AttentionCard",
    "CategorizedFlags",
    "DocumentPatternContext",
    "PatternIndex",
    "ReviewQueueCard",
    "categorize_flags",
    "derive_fraud_band",
]
