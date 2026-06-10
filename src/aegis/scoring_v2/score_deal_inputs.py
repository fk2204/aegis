"""Helpers that compute Track A + Track B inputs for ``score_deal``.

``aegis.scoring.score.score_deal`` accepts ``track_a_verdict`` and
``track_b_band`` kwargs (U30). When ``AEGIS_SCORING_ENGINE=track_abc``
the score path consumes those verdicts as the live decline gate. The
kwargs default to ``None``; under the default ``legacy`` engine they
are ignored — that's the byte-identical-to-pre-config-flag guarantee.

This module is the bridge between the existing dossier-panel pipeline
(which already computes per-document Track A verdicts + the Track B
band for display) and the score_deal call sites. It exposes:

* :func:`compute_score_deal_track_inputs` — for callers that have
  ``documents`` + a ``list_transactions`` callable + (optionally)
  pre-fetched analyses. The dossier and dashboard call sites already
  hold those, so the wiring is one extra function call per scoring
  pass.

Failure mode: any exception from the underlying ``compute_*`` calls is
swallowed and a ``(None, None)`` pair is returned. Reasoning is in
CLAUDE.md "Decision-boundary changes — shadow-first": a verdict-compute
crash MUST NOT break scoring. Falling back to ``None`` makes
``score_deal`` revert to the legacy engine for that call, which is the
documented safe behaviour. Exceptions are logged structurally so the
operator can diagnose corrupt parse_results without losing the score.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.dossier_panel import (
    UnifiedTracksView,
    build_unified_tracks_view,
)
from aegis.scoring_v2.track_a import IntegrityVerdict
from aegis.scoring_v2.track_a.models import FAIL_BRANCHES, REVIEW_BRANCHES
from aegis.scoring_v2.track_b import BusinessRiskBand


def _worst_integrity_verdict(
    verdicts: tuple[IntegrityVerdict, ...],
) -> IntegrityVerdict | None:
    """Pick the worst per-document verdict.

    ``score_deal`` consumes a single ``IntegrityVerdict``; Track A is
    per-document, so when a merchant has multiple statements the
    "worst" verdict drives the gate. Ordering: ``fail`` > ``review`` >
    ``clean``. Within a level, the first verdict in the input order
    wins (``build_unified_tracks_view`` orders by upload time desc, so
    "first" is the most recent statement — the most-relevant signal).

    Returns ``None`` when no verdicts were produced (no documents with
    integrity signals to evaluate). The caller should pass ``None``
    through to ``score_deal``.
    """
    if not verdicts:
        return None
    for v in verdicts:
        if v.branch in FAIL_BRANCHES:
            return v
    for v in verdicts:
        if v.branch in REVIEW_BRANCHES:
            return v
    return verdicts[0]


def _extract_inputs_from_view(
    view: UnifiedTracksView,
) -> tuple[IntegrityVerdict | None, BusinessRiskBand | None]:
    """Project the unified panel onto the ``score_deal`` kwarg shape."""
    return _worst_integrity_verdict(view.integrity_verdicts), view.risk_band


def compute_score_deal_track_inputs(
    *,
    documents: list[Any],
    list_transactions: Callable[[UUID], list[ClassifiedTransaction]],
    analyses_by_doc: dict[UUID, Any] | None = None,
    merchant_id: str | UUID | None = None,
) -> tuple[IntegrityVerdict | None, BusinessRiskBand | None]:
    """Compute ``(track_a_verdict, track_b_band)`` for a scoring pass.

    Reuses ``build_unified_tracks_view`` so we have one authoritative
    Track A/B lifecycle. Any exception during verdict composition logs
    a warning and returns ``(None, None)`` so ``score_deal`` falls back
    to the legacy engine (``AEGIS_SCORING_ENGINE=legacy`` byte-identical
    behaviour) rather than 500ing on a corrupt parse_result.

    Parameters
    ----------
    documents
        Every ``DocumentRow`` attached to the merchant. Empty list →
        ``(None, None)`` (nothing to evaluate; score_deal runs the
        legacy gate on the operator-provided ScoreInput).
    list_transactions
        Callable mapping ``document_id`` → list of classified txns.
        Typically ``DocumentRepository.list_transactions``.
    analyses_by_doc
        Optional pre-fetched analyses for bundle-matching (account_last4).
        Same shape ``build_unified_tracks_view`` accepts.
    merchant_id
        Logging context only — surfaces on the warning row when the
        verdict computation fails so the operator can find the offending
        merchant. Not consumed by the verdict logic.
    """
    if not documents:
        return None, None
    try:
        view = build_unified_tracks_view(
            documents=documents,
            list_transactions=list_transactions,
            analyses_by_doc=analyses_by_doc,
        )
    except Exception as exc:
        # Import deferred so module import doesn't trip the boot guard in
        # ``aegis.logger.configure_logging`` (which reads settings).
        from aegis.logger import get_logger

        get_logger(__name__).warning(
            "score_deal_track_inputs.compute_failed "
            "merchant_id=%s err=%s",
            merchant_id,
            exc.__class__.__name__,
        )
        return None, None
    return _extract_inputs_from_view(view)


__all__ = [
    "compute_score_deal_track_inputs",
]
