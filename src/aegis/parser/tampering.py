"""Bank-statement tampering composition rule.

The named hard-decline reason ``bank_statement_tampering_confirmed`` was
previously dead in production: wired through ``ScoreInput``, exercised
in tests, but no code path ever set the field to True. This module is
its real-world wiring.

Policy (balanced auto-decline, picked by the operator):

* **Strong branch** — ``metadata_score >= 50`` fires alone. Hard editor
  fingerprints (Foxit, Nitro, …), forged authors, or stripped/structural
  metadata anomalies at this score level are unambiguous enough to act
  on without corroboration.

* **Medium branch** — ``25 <= metadata_score <= 49`` fires only when
  corroborated by a math/structural validation failure: broken period
  reconciliation, broken intraday running balance, broken deposit /
  withdrawal totals, or future-dated period. Pure behavioral patterns
  (customer concentration, payroll absence, …) do NOT corroborate —
  that's the VU-shaped false-positive guard. A legitimate single-big-
  customer merchant who opened their PDF in Preview must not auto-
  decline.

Decline mode is governed by ``settings.aegis_tampering_decline_mode``:

* ``"shadow"`` (default) — pipeline computes the evaluation and writes a
  ``tampering_would_decline`` audit row, but does NOT set
  ``tampering_confirmed`` on the score input. The deal scores as if
  this module did not exist. Used to measure real true-positive /
  false-positive behavior on the operator's live corpus before any
  applicant gets rejected.

* ``"live"`` — same evaluation + audit (action ``tampering_decline_applied``),
  PLUS ``score_input_multi_month`` reads the latest document's persisted
  fraud_score_breakdown and re-evaluates from the persisted scores so
  ``tampering_confirmed`` becomes True. ``score.py`` then surfaces
  ``bank_statement_tampering_confirmed`` as a hard decline.

Scope guard: this module computes a SIGNAL. It does not write to the
analyses table, does not push to the dossier, does not modify scoring
weights. Storage + presentation live in their existing modules; this
module only emits an evaluation that those modules consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

# Validation-failure prefixes that count as "math is broken" — i.e.
# arithmetic that doesn't add up. Behavioral patterns (concentration,
# payroll, kiting, …) live in patterns.py and are intentionally
# excluded — they are not tampering corroboration.
_CORROBORATING_PREFIXES: Final[tuple[str, ...]] = (
    "reconciliation_failed",
    "future_dated",
)

# Score band thresholds. Mirror the operator-specified composition.
_STRONG_METADATA_FLOOR: Final[int] = 50
_MEDIUM_METADATA_FLOOR: Final[int] = 25
_MEDIUM_METADATA_CEIL: Final[int] = 49

# Coarser score-only re-evaluation in live mode at multi_month time
# uses ``math_score >= _CORROBORATING_MATH_SCORE`` as the proxy for
# "at least one critical reconciliation/future-dated failure" — that
# is the value ``_math_score`` returns for one critical failure (see
# ``pipeline._math_score``).
_CORROBORATING_MATH_SCORE: Final[int] = 55

TamperingBranch = Literal["strong_metadata", "medium_corroborated", "none"]


@dataclass(frozen=True)
class TamperingEvaluation:
    """Output of ``evaluate_tampering``.

    ``fires`` is the bool gate; ``branch`` names which composition leg
    fired; ``contributing_failures`` is the list of validation-failure
    codes that satisfied the medium-branch corroboration (empty for the
    strong branch and for the non-firing case). ``rationale`` is the
    one-line human-readable explanation written to the shadow audit
    row's details so the operator can scan the matrix without
    re-deriving the rule.
    """

    fires: bool
    branch: TamperingBranch
    metadata_score: int
    math_score: int
    contributing_failures: list[str] = field(default_factory=list)
    rationale: str = ""


def evaluate_tampering(
    *,
    metadata_score: int,
    math_score: int,
    validation_failures: list[str],
) -> TamperingEvaluation:
    """Compute the tampering composition for one parse.

    Pure function over the three inputs already produced upstream
    (metadata score from ``analyze_metadata``, math score from
    ``pipeline._math_score``, validation failures from
    ``validate_extraction``). No side effects; no I/O.
    """
    if metadata_score >= _STRONG_METADATA_FLOOR:
        return TamperingEvaluation(
            fires=True,
            branch="strong_metadata",
            metadata_score=metadata_score,
            math_score=math_score,
            contributing_failures=[],
            rationale=(
                f"metadata_score={metadata_score} >= {_STRONG_METADATA_FLOOR} "
                "(strong: hard editor / forged author / structural anomaly)"
            ),
        )

    if _MEDIUM_METADATA_FLOOR <= metadata_score <= _MEDIUM_METADATA_CEIL:
        corroborating = [
            f
            for f in validation_failures
            if f.startswith(_CORROBORATING_PREFIXES)
        ]
        if corroborating:
            return TamperingEvaluation(
                fires=True,
                branch="medium_corroborated",
                metadata_score=metadata_score,
                math_score=math_score,
                contributing_failures=corroborating,
                rationale=(
                    f"metadata_score={metadata_score} "
                    f"(medium, {_MEDIUM_METADATA_FLOOR}-{_MEDIUM_METADATA_CEIL}) "
                    f"+ math/structural failure: {corroborating[0]}"
                ),
            )

    return TamperingEvaluation(
        fires=False,
        branch="none",
        metadata_score=metadata_score,
        math_score=math_score,
        contributing_failures=[],
        rationale=(
            f"below thresholds — metadata_score={metadata_score}, "
            f"math_score={math_score}, "
            f"corroborating_failures={len(validation_failures)}"
        ),
    )


def evaluate_tampering_from_scores(
    *,
    metadata_score: int,
    math_score: int,
) -> bool:
    """Coarse-grained re-evaluation from persisted scores only.

    Used by ``score_input_multi_month`` in live mode where the raw
    validation-failure list is not available — only the fraud_score
    breakdown ints from the documents row. The math_score >= 55 proxy
    fires only when at least one critical failure (reconciliation /
    future-dated / extraction-truncated) was emitted at parse time
    (see ``pipeline._math_score``). extraction_truncated specifically
    is a technical "couldn't see all the data" rather than a tampering
    signal — but in this coarse path we can't distinguish, so the
    medium-branch live-mode rule is slightly broader than the parse-
    time rule by design (fail-closed). The strong branch is identical.
    """
    if metadata_score >= _STRONG_METADATA_FLOOR:
        return True
    if (
        _MEDIUM_METADATA_FLOOR <= metadata_score <= _MEDIUM_METADATA_CEIL
        and math_score >= _CORROBORATING_MATH_SCORE
    ):
        return True
    return False


__all__ = [
    "TamperingBranch",
    "TamperingEvaluation",
    "evaluate_tampering",
    "evaluate_tampering_from_scores",
]
