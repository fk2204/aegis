"""Test the fraud-cluster triangulation rule in pipeline.py.

Three+ independent patterns where at least one has severity >= 25 →
the pipeline emits a `[COMPOUND] fraud_cluster_triangulated:N_signals`
flag and bumps patterns_score by +10 (capped at 100).

Single isolated patterns are routine; clusters of three are not.
"""

from __future__ import annotations

from aegis.parser.patterns import Pattern, PatternAnalysis
from aegis.parser.pipeline import _fraud_cluster_triangulation


def _analysis(*patterns: Pattern) -> PatternAnalysis:
    return PatternAnalysis(
        patterns=list(patterns),
        mca_positions=[],
        has_kiting=False,
        paydown_suspected=False,
    )


def test_triangulation_silent_with_two_patterns() -> None:
    a = Pattern(code="mca_stacking", severity=30, detail="")
    b = Pattern(code="duplicate_deposits_detected", severity=30, detail="")
    assert _fraud_cluster_triangulation(_analysis(a, b)) is None


def test_triangulation_fires_with_three_patterns_one_severe() -> None:
    a = Pattern(code="mca_stacking", severity=30, detail="")
    b = Pattern(code="round_number_deposits", severity=15, detail="")
    c = Pattern(code="recent_account_opening", severity=15, detail="")
    result = _fraud_cluster_triangulation(_analysis(a, b, c))
    assert result is not None
    assert "fraud_cluster_triangulated:3_signals" in result
    assert "mca_stacking" in result


def test_triangulation_silent_when_no_pattern_above_25_severity() -> None:
    """3 patterns but all low severity (< 25) → not a triangulated cluster."""
    a = Pattern(code="round_number_deposits", severity=15, detail="")
    b = Pattern(code="recent_account_opening", severity=15, detail="")
    c = Pattern(code="nsf_clustering_short", severity=20, detail="")
    assert _fraud_cluster_triangulation(_analysis(a, b, c)) is None


def test_triangulation_silent_with_none_patterns() -> None:
    assert _fraud_cluster_triangulation(None) is None
