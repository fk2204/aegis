"""Tests for ``aegis.scoring_v2.score_for_sync``.

Only the trivial accessor is unit-tested here. The
``compute_score_result_for_default_bundle`` pipeline is exercised
indirectly through the sync-route integration tests.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aegis.scoring.models import ScoreResult
from aegis.scoring_v2.score_for_sync import recommended_factor_rate_from


def _score_result(
    *, factor: Decimal, recommendation: str = "approve", tier: str = "A"
) -> ScoreResult:
    """Build a ScoreResult with the field-of-interest pinned and the
    rest at neutral defaults."""
    return ScoreResult(
        score=70,
        tier=tier,
        recommendation=recommendation,
        recommended_factor_rate=factor,
    )


def test_none_score_result_returns_none() -> None:
    assert recommended_factor_rate_from(None) is None


def test_factor_above_floor_returns_factor() -> None:
    """A real recommendation (>1.0) comes through unchanged."""
    result = _score_result(factor=Decimal("1.180"))
    assert recommended_factor_rate_from(result) == Decimal("1.180")


def test_factor_at_floor_returns_none() -> None:
    """``1.0`` is the boundary — equal to the floor means no
    recommendation (no factor margin). Strict ``<=`` on the gate."""
    result = _score_result(factor=Decimal("1.0"))
    assert recommended_factor_rate_from(result) is None


def test_hard_decline_zero_factor_returns_none() -> None:
    """Hard-decline path produces ``recommended_factor_rate = 0.00``.
    The accessor folds this into the ``None`` semantics so downstream
    callers (Close sync, dossier submissions form) handle both as
    "no recommendation"."""
    result = _score_result(factor=Decimal("0.00"), recommendation="decline", tier="F")
    assert recommended_factor_rate_from(result) is None


@pytest.mark.parametrize("factor", [Decimal("0.5"), Decimal("0.99"), Decimal("0.999")])
def test_sub_floor_factor_returns_none(factor: Decimal) -> None:
    """Any sub-1.0 factor folds to ``None`` — these are
    place-of-no-recommendation values that should not be pushed to
    Close's Recommended Factor Rate."""
    result = _score_result(factor=factor)
    assert recommended_factor_rate_from(result) is None
