"""Pipeline threshold boundary + compound-signal escalation tests.

The legacy ``_fraud_score`` weighted-sum + compound-signal escalation
ladder is still exercised here because the audit / portfolio analytics
still surface ``fraud_score`` informationally. As of 2026-06-25 the
parser-layer routing decisions no longer read ``fraud_score`` — see
``test_track_a_prescreen.py`` for the Track A integrity pre-screen that
replaced the ``fraud_score >= HARD_DECLINE_THRESHOLD`` /
``metadata.fraud_score >= METADATA_HARD_DECLINE`` gates.

These tests pin the weighted-sum math so a future change that would
silently shift the informational ``fraud_score`` shows up here.
"""

from __future__ import annotations

from aegis.parser.pipeline import (
    HARD_DECLINE_THRESHOLD,
    REVIEW_THRESHOLD,
    TRACK_A_PRESCREEN_THRESHOLD,
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
    score, _, compound = _fraud_score(metadata_score=50, math_score=0, patterns_score=40)
    assert score >= HARD_DECLINE_THRESHOLD
    assert any("metadata+patterns" in c for c in compound)


def test_compound_just_below_metadata_threshold_does_not_escalate() -> None:
    """metadata = 49 (one below 50) AND patterns = 40 should NOT escalate via this rule."""
    score, _, compound = _fraud_score(metadata_score=49, math_score=0, patterns_score=40)
    # Weighted: 49 * 0.35 + 0 + 40 * 0.25 = 17.15 + 10 = 27.15 → round 27
    assert score == 27
    assert all("metadata+patterns" not in c for c in compound)


def test_compound_math_patterns_escalates_above_hard_decline() -> None:
    """math ≥ 55 AND patterns ≥ 40 escalates to HARD_DECLINE_THRESHOLD + 5."""
    score, _, compound = _fraud_score(metadata_score=0, math_score=55, patterns_score=40)
    assert score >= HARD_DECLINE_THRESHOLD + 5
    assert any("math_failure+patterns" in c for c in compound)


def test_three_layer_convergence_escalates_above_hard_decline() -> None:
    """metadata ≥ 40 AND math ≥ 40 AND patterns ≥ 30 escalates."""
    score, _, compound = _fraud_score(metadata_score=40, math_score=40, patterns_score=30)
    assert score >= HARD_DECLINE_THRESHOLD + 5
    assert any("three-layer" in c for c in compound)


def test_patterns_alone_above_80_escalates() -> None:
    """Patterns ≥ 80 alone bumps to HARD_DECLINE_THRESHOLD + 5 even with no other signals."""
    score, _, _compound = _fraud_score(metadata_score=0, math_score=0, patterns_score=80)
    assert score >= HARD_DECLINE_THRESHOLD + 5


def test_threshold_constants_sanity() -> None:
    """The legacy fraud_score scale relies on REVIEW < HARD_DECLINE.

    ``HARD_DECLINE_THRESHOLD`` is the backwards-compatible alias for
    ``TRACK_A_PRESCREEN_THRESHOLD`` (audit §A.2). Both must stay above
    ``REVIEW_THRESHOLD`` for the escalation ladder above to round-trip
    cleanly. The aliasing — same numeric value — is part of the
    contract the lookback scripts + legacy scoring engine depend on.
    """
    assert REVIEW_THRESHOLD < HARD_DECLINE_THRESHOLD
    assert HARD_DECLINE_THRESHOLD == TRACK_A_PRESCREEN_THRESHOLD
