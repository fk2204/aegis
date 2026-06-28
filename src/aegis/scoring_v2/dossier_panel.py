"""Unified A+B+C dossier panel — data assembly only.

Reads what the existing dossier handler already has (the merchant's
documents + analyses + transactions) and produces a single
``UnifiedTracksView`` the template renders.

PURE PRESENTATION SUPPORT. This module does not change the live decline
path, does not modify ``fraud_score``, does not write to any database.
The existing scoring block on the dossier renders unchanged alongside
this panel — Step 2 is the deliberate flip when the operator directs
it; this commit just adds the A/B/C surface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.counterparty import classify_bundle
from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.industry import IndustryTier

if TYPE_CHECKING:
    from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.track_a import (
    DocumentIntegritySignals,
    IntegrityVerdict,
    compute_integrity_verdict,
)
from aegis.scoring_v2.track_a.models import (
    FAIL_BRANCHES,
    REVIEW_BRANCHES,
    VerdictLevel,
)
from aegis.scoring_v2.track_b import (
    BusinessRiskBand,
    compute_risk_band,
)
from aegis.scoring_v2.track_c import (
    ConcentrationContextPanel,
    compute_context_panel,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class UnifiedTracksView(_StrictModel):
    """The combined A+B+C output the dossier template consumes.

    Rendering order is INTENTIONAL: integrity first (it's the gate),
    then the band, then the concentration panel. The template walks
    this object top-to-bottom.
    """

    integrity_verdicts: tuple[IntegrityVerdict, ...] = Field(
        description=(
            "One verdict per document — Track A is per-document. "
            "Ordered by document upload time so the most recent "
            "statement is first."
        ),
    )
    integrity_worst_verdict: VerdictLevel | None = Field(
        default=None,
        description=(
            "The worst per-document verdict (fail > review > clean). "
            "Drives the headline chip the underwriter sees first. "
            "None when no documents were available for Track A."
        ),
    )
    integrity_summary: str = Field(
        default="",
        max_length=160,
        description=(
            "One-line roll-up: 'All 4 documents clean' / "
            "'1 fail, 2 review, 1 clean' / 'Insufficient signals'. "
            "Mirrors the by-class summary Track C uses."
        ),
    )
    risk_band: BusinessRiskBand | None = Field(
        default=None,
        description=(
            "Track B output. None when no documents in the merchant "
            "have classified transactions to aggregate."
        ),
    )
    context_panel: ConcentrationContextPanel | None = Field(
        default=None,
        description=(
            "Track C output. None for the same reason as risk_band — "
            "the two share the bundle aggregation."
        ),
    )
    industry_tier: IndustryTier | None = Field(
        default=None,
        description=(
            "Industry risk classification for the merchant — derived "
            "from ``merchant.industry_choice`` via "
            "``aegis.scoring_v2.industry.industry_risk_tier``. Drives "
            "the dossier industry chip; ``None`` when caller didn't "
            "thread industry data (legacy merchants, sync-side calls "
            "pre-migration 055)."
        ),
    )
    industry_tier_reason: str = Field(
        default="",
        max_length=240,
        description=(
            "One-line underwriter-voice explanation for the tier, "
            "rendered as the chip qualifier. Empty when "
            "``industry_tier`` is None."
        ),
    )
    insufficient_data_reason: str = Field(
        default="",
        max_length=240,
        description=(
            "When risk_band / context_panel are None, this explains "
            "why so the underwriter sees 'no documents parsed yet' "
            "vs 'documents present but no transactions persisted' "
            "vs 'no documents on file'."
        ),
    )


def _signals_for_document(doc: Any) -> DocumentIntegritySignals:  # noqa: ANN401
    """Build a Track A input from a ``DocumentRow``.

    Reads ``metadata_flags``, ``all_flags``, and
    ``fraud_score_breakdown['metadata']``. Track A's signals module
    tolerates both the raw parser format and the persisted
    ``[MATH] `` / ``[META] `` category-prefixed form, so we pass the
    DB columns verbatim.
    """
    breakdown = getattr(doc, "fraud_score_breakdown", None) or {}
    metadata_score = 0
    if isinstance(breakdown, dict):
        meta_val = breakdown.get("metadata", 0)
        if isinstance(meta_val, int):
            metadata_score = max(0, min(100, meta_val))
    return DocumentIntegritySignals(
        document_id=str(doc.id),
        metadata_score=metadata_score,
        metadata_flags=tuple(getattr(doc, "metadata_flags", None) or ()),
        validation_failures=tuple(getattr(doc, "all_flags", None) or ()),
    )


def _summarise_verdicts(verdicts: list[IntegrityVerdict]) -> tuple[VerdictLevel | None, str]:
    """Roll up per-document verdicts to one chip-ready summary."""
    if not verdicts:
        return None, "No documents available for integrity check"
    counts: dict[VerdictLevel, int] = {"fail": 0, "review": 0, "clean": 0}
    for v in verdicts:
        counts[v.verdict] += 1
    if counts["fail"] > 0:
        worst: VerdictLevel = "fail"
    elif counts["review"] > 0:
        worst = "review"
    else:
        worst = "clean"
    parts = []
    for level in ("fail", "review", "clean"):
        if counts[level]:
            parts.append(f"{counts[level]} {level}")
    return worst, ", ".join(parts)


def build_unified_tracks_view(
    *,
    documents: list[Any],
    list_transactions: Callable[[UUID], list[ClassifiedTransaction]],
    analyses_by_doc: dict[UUID, Any] | None = None,
    industry_tier: IndustryTier | None = None,
    merchant: MerchantRow | None = None,
) -> UnifiedTracksView:
    """Assemble the A+B+C view for a merchant dossier.

    Parameters
    ----------
    documents
        Every ``DocumentRow`` attached to the merchant — including
        manual_review and error states. Track A operates on each
        document that has metadata or validation flags; Track B + C
        operate on the bundle of documents that have classified
        transactions persisted.
    list_transactions
        Callable that returns ``list[ClassifiedTransaction]`` for a
        document id. Typically ``docs.list_transactions``. Called once
        per document with parsed transactions.
    analyses_by_doc
        Optional pre-fetched analyses, keyed by document id, to pull
        ``account_last4`` for bundle-matching. When omitted the
        bundle's account-last4 set defaults to empty (own_account
        matching still pairs by Confirmation# + opposite-sign +
        equal-magnitude regardless).

    Returns
    -------
    UnifiedTracksView
        Ready for the template to render. ``risk_band`` and
        ``context_panel`` are ``None`` when no transactions are
        available; the template renders an empty-state message in
        that case.
    """
    # ── Track A: one verdict per document that has any integrity
    #            signal source (metadata_flags or all_flags). Sort
    #            by upload time descending so the most recent doc
    #            renders first.
    #
    # F7 (INFO, docs/track_a_audit_2026-06-12.md): the
    # ``getattr(...) or ""`` fallback below tolerates two shapes
    # because the test stubs (`_document_row_stub` in
    # `tests/scoring_v2/test_dossier_panel.py`) pass `uploaded_at` as
    # an ISO-8601 STRING while prod `DocumentRow` declares it as a
    # required `datetime` (`storage.py:99`). String-string compare and
    # datetime-datetime compare both work; the `or ""` only ever
    # triggers on the (hypothetical) None case — where it would mask a
    # real bug rather than crash the sort. Kept intentionally for
    # backwards-compat with the existing test stubs; if the stubs are
    # ever upgraded to the typed `DocumentRow`, drop the `or ""` so
    # mypy + a None at runtime would surface a TypeError instead of
    # silently mis-ordering.
    sorted_docs = sorted(
        documents,
        key=lambda d: getattr(d, "uploaded_at", None) or "",
        reverse=True,
    )
    integrity_verdicts: list[IntegrityVerdict] = []
    for d in sorted_docs:
        signals = _signals_for_document(d)
        if (
            signals.metadata_score == 0
            and not signals.metadata_flags
            and not signals.validation_failures
        ):
            # No signals to evaluate — skip the doc so the panel doesn't
            # show a meaningless "clean" verdict for an unparsed row.
            continue
        integrity_verdicts.append(compute_integrity_verdict(signals))

    worst, integrity_summary = _summarise_verdicts(integrity_verdicts)

    # ── Track B + C: load transactions for each doc that has them.
    #                Both tracks share the same bundle/aggregation; we
    #                only walk the document list once.
    #
    # Exception handling: previously this loop swallowed ALL exceptions
    # silently and fell back to ``txns = []``. That masked a class of
    # bugs where a SINGLE malformed transaction row (e.g. a row whose
    # ``source_page`` came back NULL, a description that failed
    # ``min_length=1`` after strip, a category not in the ``TransactionCategory``
    # Literal) crashed ``list_transactions``'s list-comprehension mapper —
    # dropping the ENTIRE document's transactions, every time. When this
    # happened on every doc for a merchant, ``transactions_by_doc`` came
    # out empty, ``risk_band`` was set to ``None``, and the dossier
    # rendered "Documents present but no classified transactions are
    # persisted" — looking like a parser-write bug when it was actually a
    # row-mapper read failure. We still fall back to ``txns = []`` (the
    # dossier MUST render even when one doc's transactions are corrupt),
    # but the exception is now logged with the offending document_id so
    # the operator can find and fix the underlying row, rather than
    # silently losing every Track B / Track C signal.
    transactions_by_doc: dict[str, list[ClassifiedTransaction]] = {}
    accounts: set[str] = set()
    for d in sorted_docs:
        try:
            txns = list_transactions(d.id)
        except Exception as exc:
            # Import deferred so module import doesn't trip the boot
            # guard in ``aegis.logger.configure_logging`` (mirrors the
            # pattern used in ``score_deal_inputs.py``).
            from aegis.logger import get_logger

            get_logger(__name__).warning(
                "dossier_panel.list_transactions_failed document_id=%s err=%s",
                d.id,
                exc.__class__.__name__,
            )
            txns = []
        if not txns:
            continue
        transactions_by_doc[str(d.id)] = txns
        if analyses_by_doc is not None:
            analysis = analyses_by_doc.get(d.id)
            if analysis is not None:
                last4 = getattr(analysis, "account_last4", None)
                if last4:
                    accounts.add(str(last4))

    risk_band: BusinessRiskBand | None = None
    context_panel: ConcentrationContextPanel | None = None
    insufficient_reason = ""

    if transactions_by_doc:
        classifications, _ = classify_bundle(transactions_by_doc, accounts)
        risk_band = compute_risk_band(
            transactions_by_doc,
            classifications,
            industry_tier=industry_tier,
            merchant=merchant,
        )
        context_panel = compute_context_panel(transactions_by_doc, classifications)
    else:
        if not documents:
            insufficient_reason = "No documents on file."
        elif integrity_verdicts:
            insufficient_reason = (
                "Documents present but no classified transactions are "
                "persisted (parser may have validated for integrity but "
                "not committed transaction rows). Track B / Track C "
                "cannot compute without classified transactions."
            )
        else:
            insufficient_reason = (
                "Documents present but parser has not produced "
                "integrity or transaction signals yet."
            )

    if industry_tier is not None:
        from aegis.scoring_v2.industry import industry_tier_reason as _industry_reason

        industry_reason_text = _industry_reason(industry_tier)
    else:
        industry_reason_text = ""

    return UnifiedTracksView(
        integrity_verdicts=tuple(integrity_verdicts),
        integrity_worst_verdict=worst,
        integrity_summary=integrity_summary,
        risk_band=risk_band,
        context_panel=context_panel,
        industry_tier=industry_tier,
        industry_tier_reason=industry_reason_text,
        insufficient_data_reason=insufficient_reason,
    )


__all__ = [
    "FAIL_BRANCHES",
    "REVIEW_BRANCHES",
    "UnifiedTracksView",
    "build_unified_tracks_view",
]
