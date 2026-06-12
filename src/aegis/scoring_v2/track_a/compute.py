"""Track A verdict composition.

Pure function. Reads per-document integrity signals, applies the
Q2-decided branch logic, returns the verdict.

Branch precedence (most-severe first; first match wins):

1. **strong_metadata** — ``metadata_score >= 50``. Hard editor /
   forged author / structural anomaly is unambiguous enough to fail
   without corroboration. Mirrors
   ``aegis.parser.tampering._STRONG_METADATA_FLOOR``.
2. **drift_plus_editor** — running-balance drift AND editor
   metadata both present. The competent-fabrication signature
   (A&R KM's iText 2.1.7 + 4-month reconciliation drift). Fires
   regardless of the raw ``metadata_score`` because the
   corroboration is the signal — the editor flag presence + the
   math failure together are the integrity evidence, even if
   the score itself is in the medium band.
3. **medium_corroborated** — ``25 <= metadata_score <= 49`` AND at
   least one math/structural reconciliation failure (but no
   editor metadata present, otherwise branch 2 would have fired).
   Mirrors ``aegis.parser.tampering._MEDIUM_METADATA_*``.
4. **drift_alone** — reconciliation drift WITHOUT editor metadata
   AND ``metadata_score < 25``. Could be a genuine OCR or parser
   miss; surfaces as review for the underwriter to adjudicate.
5. **clean** — nothing fired.

The function never modifies the input. It does not call the LLM, the
parser, or the database — fully deterministic and unit-testable.
"""

from __future__ import annotations

from aegis.scoring_v2.track_a.framing import (
    frame_drift_alone,
    frame_drift_plus_editor,
    frame_medium_corroborated,
    frame_strong_metadata,
)
from aegis.scoring_v2.track_a.models import (
    DocumentIntegritySignals,
    EvidenceItem,
    IntegrityVerdict,
)
from aegis.scoring_v2.track_a.signals import (
    _strip_category_prefix,
    extract_drift_failures,
    extract_editor_metadata_flag,
    extract_other_metadata_flags,
)

# Thresholds. Re-declared (not imported from parser.tampering) so this
# module has no implicit dependency on the parser package. The values
# stay in sync with ``parser.tampering._STRONG_METADATA_FLOOR`` and
# ``_MEDIUM_METADATA_FLOOR/_CEIL`` — a deliberate code-level invariant
# (a guard test asserts the equivalence).
_STRONG_METADATA_FLOOR: int = 50
_MEDIUM_METADATA_FLOOR: int = 25
_MEDIUM_METADATA_CEIL: int = 49


def compute_integrity_verdict(
    signals: DocumentIntegritySignals,
) -> IntegrityVerdict:
    """Compute the Track A verdict for one document.

    See module docstring for branch precedence.
    """
    drift_failures = extract_drift_failures(signals.validation_failures)
    editor_flag = extract_editor_metadata_flag(signals.metadata_flags)
    other_meta = extract_other_metadata_flags(signals.metadata_flags)

    # ── Branch 1: strong metadata (fail) ──────────────────────────
    if signals.metadata_score >= _STRONG_METADATA_FLOOR:
        evidence = [
            EvidenceItem(
                signal="metadata_score",
                detail=(
                    f"metadata_score={signals.metadata_score} "
                    f">= {_STRONG_METADATA_FLOOR} "
                    "(strong: hard editor / forged author / structural)"
                ),
            ),
        ]
        # Surface ALL metadata flags as supporting evidence so the
        # underwriter sees what specifically tripped the score.
        if editor_flag is not None:
            evidence.append(
                EvidenceItem(signal="editor_detected", detail=editor_flag)
            )
        for f in other_meta:
            evidence.append(EvidenceItem(signal="metadata_flag", detail=f))
        # Reconciliation drift, if present, IS corroborating evidence for
        # a strong-metadata fail. Surface each failure as its own row so
        # the underwriter sees the full pattern (mirrors branch 2 / 3 /
        # 4 behavior) instead of mistaking a strong-metadata fail for
        # "just metadata noise" and softening it to review.
        for f in drift_failures:
            evidence.append(
                EvidenceItem(signal=_drift_signal_token(f), detail=f)
            )
        return IntegrityVerdict(
            document_id=signals.document_id,
            verdict="fail",
            branch="strong_metadata",
            metadata_score=signals.metadata_score,
            evidence=tuple(evidence),
            rationale=frame_strong_metadata(signals.metadata_score),
        )

    # ── Branch 2: drift + editor metadata (fail) ──────────────────
    # Fires when BOTH the editor flag AND any reconciliation drift
    # are present, regardless of metadata_score (since the signals
    # already corroborate). This is the "competent fabrication" case.
    if editor_flag is not None and drift_failures:
        evidence = [EvidenceItem(signal="editor_detected", detail=editor_flag)]
        for f in drift_failures:
            evidence.append(
                EvidenceItem(
                    signal=_drift_signal_token(f),
                    detail=f,
                )
            )
        return IntegrityVerdict(
            document_id=signals.document_id,
            verdict="fail",
            branch="drift_plus_editor",
            metadata_score=signals.metadata_score,
            evidence=tuple(evidence),
            rationale=frame_drift_plus_editor(
                editor_flag, len(drift_failures), signals.metadata_score
            ),
        )

    # ── Branch 3: medium metadata + math failure (review) ─────────
    if (
        _MEDIUM_METADATA_FLOOR
        <= signals.metadata_score
        <= _MEDIUM_METADATA_CEIL
        and drift_failures
    ):
        evidence = [
            EvidenceItem(
                signal="metadata_score",
                detail=(
                    f"metadata_score={signals.metadata_score} "
                    f"(medium, {_MEDIUM_METADATA_FLOOR}-{_MEDIUM_METADATA_CEIL})"
                ),
            ),
        ]
        for f in drift_failures:
            evidence.append(
                EvidenceItem(
                    signal=_drift_signal_token(f),
                    detail=f,
                )
            )
        for f in other_meta:
            evidence.append(EvidenceItem(signal="metadata_flag", detail=f))
        return IntegrityVerdict(
            document_id=signals.document_id,
            verdict="review",
            branch="medium_corroborated",
            metadata_score=signals.metadata_score,
            evidence=tuple(evidence),
            rationale=frame_medium_corroborated(
                signals.metadata_score, len(drift_failures)
            ),
        )

    # ── Branch 4: drift alone (review) ────────────────────────────
    if drift_failures:
        evidence = []
        for f in drift_failures:
            evidence.append(
                EvidenceItem(
                    signal=_drift_signal_token(f),
                    detail=f,
                )
            )
        return IntegrityVerdict(
            document_id=signals.document_id,
            verdict="review",
            branch="drift_alone",
            metadata_score=signals.metadata_score,
            evidence=tuple(evidence),
            rationale=frame_drift_alone(
                len(drift_failures), signals.metadata_score
            ),
        )

    # ── Branch 5: clean ───────────────────────────────────────────
    return IntegrityVerdict(
        document_id=signals.document_id,
        verdict="clean",
        branch="clean",
        metadata_score=signals.metadata_score,
        evidence=(),
        rationale=(
            f"No integrity signals fired (metadata_score="
            f"{signals.metadata_score}, no reconciliation drift, "
            "no editor metadata)."
        ),
    )


def _drift_signal_token(failure: str) -> str:
    """Map a verbatim reconciliation failure to its short signal token.

    Used to populate ``EvidenceItem.signal`` so dossier filtering by
    signal class (e.g. "show me all running-balance failures") works.
    Strips the persistence-time ``[MATH] `` / ``[META] `` etc. prefix
    if present so the token is the same regardless of input source.
    """
    head = _strip_category_prefix(failure).split(":", 1)[0]
    return head


__all__ = ["compute_integrity_verdict"]
