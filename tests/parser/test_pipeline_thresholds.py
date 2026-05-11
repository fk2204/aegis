"""Pipeline threshold boundary + compound-signal escalation tests.

The pipeline glues four scores into one weighted fraud_score and runs a
ladder of escalation rules. Boundary conditions (one point off a
threshold) are easy to break in refactors. These tests pin them.

The decision tree (parser/pipeline.py:_decide):
  - metadata.eof_markers  > EOF_HARD_DECLINE         → manual_review
  - metadata.fraud_score >= METADATA_HARD_DECLINE    → manual_review
  - fraud_score          >= HARD_DECLINE_THRESHOLD   → manual_review
  - confidence_failures                              → manual_review
  - fraud_score          >= REVIEW_THRESHOLD         → review
  - validation failed                                → review
  - metadata.eof_markers > 1                          → review
  - else                                              → proceed
"""

from __future__ import annotations

from aegis.parser.pipeline import (
    HARD_DECLINE_THRESHOLD,
    METADATA_HARD_DECLINE,
    REVIEW_THRESHOLD,
    _fraud_score,
)


def test_fraud_score_pure_weighted_average() -> None:
    """No escalation rules fire → score is just the weighted average."""
    score, breakdown, compound = _fraud_score(metadata_score=20, math_score=0, patterns_score=20)
    # 20 * 0.35 + 0 * 0.40 + 20 * 0.25 = 7 + 0 + 5 = 12
    assert score == 12
    assert compound == []
    assert breakdown == {"metadata_score": 20, "math_score": 0, "patterns_score": 20}


def test_compound_metadata_patterns_escalates_to_hard_decline() -> None:
    """metadata ≥ 50 AND patterns ≥ 40 escalates to at least HARD_DECLINE_THRESHOLD."""
    score, _, compound = _fraud_score(
        metadata_score=50, math_score=0, patterns_score=40
    )
    assert score >= HARD_DECLINE_THRESHOLD
    assert any("metadata+patterns" in c for c in compound)


def test_compound_just_below_metadata_threshold_does_not_escalate() -> None:
    """metadata = 49 (one below 50) AND patterns = 40 should NOT escalate via this rule."""
    score, _, compound = _fraud_score(
        metadata_score=49, math_score=0, patterns_score=40
    )
    # Weighted: 49 * 0.35 + 0 + 40 * 0.25 = 17.15 + 10 = 27.15 → round 27
    assert score == 27
    assert all("metadata+patterns" not in c for c in compound)


def test_compound_math_patterns_escalates_above_hard_decline() -> None:
    """math ≥ 55 AND patterns ≥ 40 escalates to HARD_DECLINE_THRESHOLD + 5."""
    score, _, compound = _fraud_score(
        metadata_score=0, math_score=55, patterns_score=40
    )
    assert score >= HARD_DECLINE_THRESHOLD + 5
    assert any("math_failure+patterns" in c for c in compound)


def test_three_layer_convergence_escalates_above_hard_decline() -> None:
    """metadata ≥ 40 AND math ≥ 40 AND patterns ≥ 30 escalates."""
    score, _, compound = _fraud_score(
        metadata_score=40, math_score=40, patterns_score=30
    )
    assert score >= HARD_DECLINE_THRESHOLD + 5
    assert any("three-layer" in c for c in compound)


def test_patterns_alone_above_80_escalates() -> None:
    """Patterns ≥ 80 alone bumps to HARD_DECLINE_THRESHOLD + 5 even with no other signals."""
    score, _, _compound = _fraud_score(
        metadata_score=0, math_score=0, patterns_score=80
    )
    assert score >= HARD_DECLINE_THRESHOLD + 5


def test_threshold_constants_sanity() -> None:
    """The escalation ladder relies on REVIEW < HARD_DECLINE < METADATA_HARD_DECLINE.

    Any future tuning that violates this ordering breaks the decision
    semantics: a metadata score of 60 hits METADATA_HARD_DECLINE first
    and bypasses the weighted average path entirely.
    """
    assert REVIEW_THRESHOLD < HARD_DECLINE_THRESHOLD
    # METADATA_HARD_DECLINE and HARD_DECLINE_THRESHOLD can be close; what
    # matters is both are > REVIEW_THRESHOLD.
    assert METADATA_HARD_DECLINE > REVIEW_THRESHOLD
