"""Pydantic models for Track A — Document Integrity verdict.

The output (``IntegrityVerdict``) is the {clean / review / fail}
verdict plus the evidence list that produced it. The evidence list is
the load-bearing structural feature: every verdict landing names the
specific signals that fired so an underwriter can read the verdict
and immediately see WHY.

CRITICAL — no field on this model maps to a decline boundary in any
consumer. A guard test (``test_verdict_has_no_decline_or_score_field``)
reads the Pydantic schema and asserts the absence of decline-related
fields, preventing a future accidental wiring of Track A into the
live decline path. The legacy ``fraud_score`` retains control until
Step 2 deliberately replaces it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The Q2-decided verdict taxonomy.
VerdictLevel = Literal["clean", "review", "fail"]


# Which compositional branch produced the verdict. Per the design doc
# and the existing ``aegis.parser.tampering.TamperingBranch``, plus
# the two new branches for the running-balance-drift placement
# (``drift_alone`` for review; ``drift_plus_editor`` for fail).
IntegrityBranch = Literal[
    "clean",
    "strong_metadata",  # metadata_score >= 50 → fail
    "drift_plus_editor",  # editor metadata + drift → fail
    "medium_corroborated",  # 25-49 metadata + math failure → review
    "drift_alone",  # drift, no editor metadata → review
]


# Convenient ordering for "is this branch a fail / review" lookups.
FAIL_BRANCHES: frozenset[IntegrityBranch] = frozenset({"strong_metadata", "drift_plus_editor"})
REVIEW_BRANCHES: frozenset[IntegrityBranch] = frozenset({"medium_corroborated", "drift_alone"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=False,
    )


class DocumentIntegritySignals(_StrictModel):
    """Per-document integrity signals — Track A's input shape.

    Built from the existing per-document data the parser already
    produces:

    * ``metadata_score`` and ``metadata_flags`` come from
      ``aegis.parser.metadata.analyze_metadata`` — already persisted
      on ``DocumentRow.fraud_score_breakdown`` and ``metadata_flags``.
    * ``validation_failures`` come from
      ``aegis.parser.validate.validate_extraction`` — surfaced on
      ``DocumentRow.all_flags`` (``[MATH]`` prefix) and on the parse
      pipeline's ValidationResult.

    Track A does NOT re-detect; it reads the existing signals and
    composes them into the verdict. This keeps the integrity surface
    consistent with the legacy scoring layer (no two detectors
    disagreeing on the same input).
    """

    document_id: str = Field(
        max_length=64,
        description=(
            "Stable identifier for the document this verdict applies "
            "to. Track A is per-document; bundle-level rollup happens "
            "in ``score_deal_inputs._worst_integrity_verdict`` (single "
            "deal scoring) and ``dossier_panel._summarise_verdicts`` "
            "(dossier panel rendering). The two pick the worst verdict "
            "in fail > review > clean order."
        ),
    )
    metadata_score: int = Field(
        ge=0,
        le=100,
        description=(
            "Integer 0-100 from ``MetadataAnalysis.fraud_score``. "
            "Drives the strong/medium branch composition."
        ),
    )
    metadata_flags: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Raw metadata flags from ``MetadataAnalysis.flags`` — "
            "e.g. 'editor_detected: iText 2.1.7 by 1T3XT'. "
            "Track A scans these for the ``editor_detected:`` "
            "prefix to gate the drift+editor → fail branch."
        ),
    )
    validation_failures: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Validation-failure codes from "
            "``ValidationResult.failures`` — e.g. "
            "'reconciliation_failed_period: expected …', "
            "'reconciliation_failed_daily_running_balance: …', "
            "'future_dated: period_end=2026-07-01 today=2026-06-12'. "
            "The composition rule reads these for the math/structural "
            "corroboration."
        ),
    )


class EvidenceItem(_StrictModel):
    """One signal that contributed to the verdict.

    Both ``strong_metadata`` and ``drift_plus_editor`` fails emit
    multiple evidence rows (e.g. one for the editor flag and one for
    each reconciliation failure) so the underwriter sees the full
    corroboration set rather than a single composite reason.
    """

    signal: str = Field(
        max_length=48,
        description=(
            "Short token identifying the signal class: "
            "'editor_detected', 'forged_author', 'structural_anomaly', "
            "'reconciliation_failed_period', 'reconciliation_failed_running_balance', "
            "'future_dated', 'metadata_score'."
        ),
    )
    detail: str = Field(
        max_length=240,
        description=(
            "The verbatim flag string from the parser (or a derived "
            "one-line summary for composite signals like "
            "'metadata_score=72'). Reads as the literal evidence the "
            "underwriter would cite."
        ),
    )


class IntegrityVerdict(_StrictModel):
    """Track A output. {clean, review, fail} + evidence. NO decline gate.

    Read flow for the dossier:

    1. Render ``verdict`` as the headline chip
       (green / yellow / red).
    2. Render ``branch`` as the WHY in machine form (operator-glossary
       value to mouse-over).
    3. Render ``evidence`` as the WHAT — one row per fired signal, in
       the order they corroborate the verdict.
    4. Use ``document_id`` to drill into the underlying document.

    The dossier renders this as INFORMATIONAL — the live decline
    boundary remains the legacy ``fraud_score`` until Step 2.
    """

    document_id: str
    verdict: VerdictLevel
    branch: IntegrityBranch
    metadata_score: int = Field(ge=0, le=100)
    evidence: tuple[EvidenceItem, ...] = Field(
        description=(
            "Every signal that contributed to the verdict, in "
            "operator-readable order. Empty for ``clean``."
        ),
    )
    rationale: str = Field(
        max_length=320,
        description=(
            "One-line human-readable explanation of the verdict / "
            "branch. Mirrors ``TamperingEvaluation.rationale`` so "
            "Track A's verbiage stays consistent with the existing "
            "tampering audit-row format."
        ),
    )


__all__ = [
    "FAIL_BRANCHES",
    "REVIEW_BRANCHES",
    "DocumentIntegritySignals",
    "EvidenceItem",
    "IntegrityBranch",
    "IntegrityVerdict",
    "VerdictLevel",
]
