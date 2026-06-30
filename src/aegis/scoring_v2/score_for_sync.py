"""Score-result helpers shared between the dossier render and the
operator-triggered ``/sync-to-close`` push.

Two small surfaces:

* :func:`recommended_factor_rate_from` — None-safe accessor for the
  ``ScoreResult.recommended_factor_rate`` value used by the Close
  Opportunity-side ``Recommended Factor Rate`` custom field. The
  dossier submissions form and the sync route both go through this so
  the "no recommendation" semantics (None vs ``0.00`` vs sub-1.0
  factor) stay aligned. A factor at or below ``Decimal("1.0")`` means
  the scorer didn't produce a useful recommendation (hard-decline
  path returns ``0.00``; revenue too low can return below 1.0) — the
  accessor returns ``None`` in those cases so callers can fall back
  to a request-supplied or default value.

* :func:`compute_score_result_for_default_bundle` — full
  ScoreInput → score_deal pipeline for the sync route. Picks the
  default bundle deterministically (most-populated bank/last4 pair),
  assembles ``items`` + ``pattern_analysis`` + Track A/B inputs, and
  returns ``ScoreResult | None``. The dossier handler does NOT use
  this: it has operator-selected bundle state that this helper
  intentionally ignores.

Why a sync-specific helper instead of refactoring the dossier
handler's inline compute: the dossier carries bundle picker state,
score_window, statement_coverage, and submissions flow off the same
``score_input`` — extracting that without disturbing several
unrelated dossier responsibilities is more risk than the deferral
warrants. The sync route has none of that state and can take the
default bundle without operator input.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from aegis.merchants.models import MerchantRow
from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import (
    PatternAnalysis,
    analyze_patterns,
    pattern_analysis_from_dto,
)
from aegis.scoring.models import ScoreResult
from aegis.scoring.multi_month import score_input_multi_month
from aegis.scoring.ofac import OFACClient, OFACStaleError
from aegis.scoring.score import score_deal
from aegis.scoring_v2.industry import industry_risk_tier
from aegis.scoring_v2.score_deal_inputs import compute_score_deal_track_inputs
from aegis.storage import AnalysisRow, DocumentRepository

_log = logging.getLogger(__name__)

# Cross-layer note: ``_collect_analyzed_for_merchant`` lives in the
# web-layer ``aegis.web._router_helpers`` because that's where the
# operator dashboard's bundle-picker rules live. The sync route also
# needs default-bundle picking (same rule: scoring across mixed-
# account statements double-counts cash moved between them). We
# import the function inside ``compute_score_result_for_default_bundle``
# rather than at module load to avoid the merchants-router import
# chain pulling this module mid-init (the merchants router imports
# `recommended_factor_rate_from` from here; importing
# `_router_helpers` at module-top would create a circular import).

_NO_RECOMMENDATION_FACTOR_FLOOR: Decimal = Decimal("1.0")
"""Below this the scorer didn't produce a meaningful factor rate.
Score_deal's hard-decline path returns ``0.00``; low-revenue soft
paths can return sub-1.0 placeholders. Treated as "no recommendation"
so the Close-side ``Recommended Factor Rate`` field stays untouched
by the sync."""


def recommended_factor_rate_from(score_result: ScoreResult | None) -> Decimal | None:
    """Return ``ScoreResult.recommended_factor_rate`` or ``None``.

    ``None`` is returned for any of:

    * ``score_result is None`` (merchant unscored).
    * ``recommended_factor_rate <= 1.0`` (scorer didn't produce a
      meaningful recommendation — hard-decline path, sub-floor
      revenue, etc.).

    Both the dossier submissions form and ``/sync-to-close``'s
    Opportunity push read through this to keep "no recommendation"
    semantics aligned. A divergence here used to mean the submissions
    form would fall back to operator-requested values while the sync
    pushed a literal ``0.00`` to Close — confusing to the underwriter
    and hard to diff against on the next sync.
    """
    if score_result is None:
        return None
    rate = score_result.recommended_factor_rate
    if rate <= _NO_RECOMMENDATION_FACTOR_FLOOR:
        return None
    return rate


def compute_score_result_for_default_bundle(
    *,
    merchant: MerchantRow,
    merchant_id: UUID,
    docs: DocumentRepository,
    ofac: OFACClient | None,
) -> ScoreResult | None:
    """Compute the merchant's ``ScoreResult`` for the most-populated
    bundle. Used by the operator-triggered ``/sync-to-close`` push.

    Returns ``None`` for any of:

    * Merchant not finalized (provisional / needs_manual_naming).
    * No analyzed documents (parse hasn't completed for any statement).
    * Default bundle picker returns empty items.
    * ``OFACStaleError`` raised mid-score — same fallback the dossier
      handler uses; the sync caller treats it as "no score available
      right now" rather than re-raising.

    Deliberately bypasses the operator-selected bundle state the
    dossier handler tracks: there's no operator session at sync time,
    so the default bundle (the most-populated bank/last4 pair) is the
    only deterministic choice.
    """
    if not merchant.is_finalized:
        return None

    from aegis.web._router_helpers import _collect_analyzed_for_merchant

    items = _collect_analyzed_for_merchant(docs, merchant_id, bundle=None)
    if not items:
        return None

    all_docs = docs.list_documents(merchant_id=merchant_id, limit=4 * 12)
    analyses_by_doc = docs.get_analyses_by_document_ids([d.id for d in all_docs])

    latest_doc, latest_analysis = items[0]
    latest_transactions = docs.list_transactions(latest_doc.id)
    pattern_analysis = _pattern_analysis_for_score(latest_analysis, latest_transactions)

    score_input = score_input_multi_month(merchant, items, pattern_analysis=pattern_analysis)
    track_a_verdict, track_b_band = compute_score_deal_track_inputs(
        documents=all_docs,
        list_transactions=docs.list_transactions,
        analyses_by_doc=analyses_by_doc,
        merchant_id=merchant_id,
        industry_tier=industry_risk_tier(merchant.industry_choice),
    )
    try:
        return score_deal(
            score_input,
            ofac=ofac,
            track_a_verdict=track_a_verdict,
            track_b_band=track_b_band,
        )
    except OFACStaleError:
        return None


def _pattern_analysis_for_score(
    analysis: AnalysisRow,
    transactions: list[ClassifiedTransaction],
) -> PatternAnalysis | None:
    """Mirror of ``aegis.web.routers.merchants._dossier_pattern_analysis``
    for the sync-side compute: prefer the stored ``pattern_analysis``
    DTO on the analysis row (migration 032+), fall back to live
    ``analyze_patterns`` only when the cached DTO is missing.

    Recompute is wrapped in ``try / except -> None`` — same defensive
    posture the dossier path uses against a single malformed
    statement crashing the sync. The sync still pushes the
    cashflow-snapshot fields it has; ``track_b_band`` ends up ``None``
    inside ``score_deal``'s ``track_abc`` path which is the documented
    "no signal" fallback.
    """
    if analysis.pattern_analysis is not None:
        return pattern_analysis_from_dto(analysis.pattern_analysis)
    try:
        return analyze_patterns(
            transactions,
            analysis.statement_period_start,
            analysis.statement_period_end,
        )
    except Exception as exc:
        _log.warning("score_for_sync.pattern_analyze_failed", exc_info=exc)
        return None


__all__ = [
    "compute_score_result_for_default_bundle",
    "recommended_factor_rate_from",
]
