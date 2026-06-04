"""Tampering composition rule — unit tests.

The composition is policy code: the operator picked the thresholds
(balanced auto-decline, 2026-06-04). These tests lock the named branch
behavior so a future refactor can't quietly shift them, AND lock the
explicit false-positive guard against behavioral patterns being treated
as corroboration (the VU Development shape from 2026-06-03).
"""

from __future__ import annotations

import pytest

from aegis.parser.tampering import (
    TamperingEvaluation,
    evaluate_tampering,
    evaluate_tampering_from_scores,
)

# ----------------------------------------------------------------------
# evaluate_tampering — strong branch (metadata >= 50)
# ----------------------------------------------------------------------


def test_strong_branch_fires_on_metadata_50() -> None:
    """metadata_score == 50 is the floor and fires alone, no corroboration."""
    result = evaluate_tampering(
        metadata_score=50, math_score=0, validation_failures=[]
    )
    assert result.fires is True
    assert result.branch == "strong_metadata"
    assert result.contributing_failures == []
    assert "strong" in result.rationale


def test_strong_branch_fires_on_metadata_100() -> None:
    result = evaluate_tampering(
        metadata_score=100, math_score=0, validation_failures=[]
    )
    assert result.fires is True
    assert result.branch == "strong_metadata"


def test_strong_branch_doesnt_need_corroboration() -> None:
    """Strong metadata fires even with zero math/structural failures."""
    result = evaluate_tampering(
        metadata_score=75, math_score=0, validation_failures=[]
    )
    assert result.fires is True
    assert result.branch == "strong_metadata"


# ----------------------------------------------------------------------
# evaluate_tampering — medium branch (25-49 + math corroboration)
# ----------------------------------------------------------------------


def test_medium_branch_fires_on_reconciliation_corroboration() -> None:
    """Medium metadata (Foxit/Preview-ish, 25-49) + broken reconciliation
    is the canonical "real fake" case."""
    result = evaluate_tampering(
        metadata_score=35,
        math_score=55,
        validation_failures=[
            "reconciliation_failed_period: expected 12345.00 got 9999.00"
        ],
    )
    assert result.fires is True
    assert result.branch == "medium_corroborated"
    assert len(result.contributing_failures) == 1


def test_medium_branch_fires_on_future_dated_corroboration() -> None:
    result = evaluate_tampering(
        metadata_score=40,
        math_score=55,
        validation_failures=["future_dated: period_end=2099-01-01 today=2026-06-04"],
    )
    assert result.fires is True
    assert result.branch == "medium_corroborated"


def test_medium_branch_fires_on_intraday_balance_corroboration() -> None:
    """Impossible running balance (the per-day reconciliation check) is
    one of the operator's specifically-named corroborating signals."""
    result = evaluate_tampering(
        metadata_score=30,
        math_score=55,
        validation_failures=["reconciliation_failed_intraday: 2026-03-15 -100.00"],
    )
    assert result.fires is True
    assert result.branch == "medium_corroborated"


# ----------------------------------------------------------------------
# False-positive guard — behavioral patterns must NOT corroborate
# (this is the VU Development shape, 2026-06-03)
# ----------------------------------------------------------------------


def test_medium_metadata_alone_does_not_fire() -> None:
    """Foxit/Preview-saved PDF with NO math failures = does not fire.
    Owner exports a real statement and saves through Preview before
    sending — perfectly legitimate."""
    result = evaluate_tampering(
        metadata_score=35,
        math_score=0,
        validation_failures=[],
    )
    assert result.fires is False
    assert result.branch == "none"


def test_medium_metadata_plus_concentration_does_not_fire() -> None:
    """VU Development shape — medium metadata + customer-concentration
    pattern. The behavioral pattern is NOT a corroborating signal. The
    deal must NOT auto-decline."""
    result = evaluate_tampering(
        metadata_score=35,
        math_score=0,
        validation_failures=[
            # patterns.py emits these via the pipeline's `all_flags`
            # collection, NOT the validation.failures stream — but
            # even if a future refactor leaked a pattern code into
            # the failures list, the prefix gate would still block it.
            "customer_concentration",
            "preloan_spike",
        ],
    )
    assert result.fires is False
    assert result.branch == "none"


def test_medium_metadata_plus_payroll_absent_does_not_fire() -> None:
    """Same shape with payroll-absent: not corroboration."""
    result = evaluate_tampering(
        metadata_score=30,
        math_score=0,
        validation_failures=["payroll_absent"],
    )
    assert result.fires is False


def test_strong_branch_metadata_49_does_not_fire_alone() -> None:
    """Boundary: 49 is the medium ceiling, NOT strong. Needs corroboration."""
    result = evaluate_tampering(
        metadata_score=49, math_score=0, validation_failures=[]
    )
    assert result.fires is False
    assert result.branch == "none"


def test_below_medium_floor_never_fires() -> None:
    """metadata_score < 25 doesn't fire even with broken reconciliation
    (a single noisy validation failure on a clean-metadata statement is
    much more likely a parser quirk than tampering)."""
    result = evaluate_tampering(
        metadata_score=20,
        math_score=85,
        validation_failures=[
            "reconciliation_failed_period: expected X got Y",
            "reconciliation_failed_deposit_total: listed X got Y",
        ],
    )
    assert result.fires is False
    assert result.branch == "none"


# ----------------------------------------------------------------------
# evaluate_tampering_from_scores — live-mode coarse path
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata_score,math_score,expected",
    [
        # Strong branch identical to the parse-time path
        (50, 0, True),
        (75, 0, True),
        (100, 0, True),
        # Below strong, below medium floor — never fires
        (24, 100, False),
        (0, 0, False),
        # Medium band needs math_score >= 55 corroboration
        (25, 55, True),
        (35, 55, True),
        (49, 55, True),
        (49, 54, False),
        (49, 0, False),
        # Boundary: 50 is strong, 49 is medium
        (50, 0, True),
        (49, 100, True),
    ],
)
def test_evaluate_from_scores_score_only_path(
    metadata_score: int, math_score: int, expected: bool
) -> None:
    assert (
        evaluate_tampering_from_scores(
            metadata_score=metadata_score, math_score=math_score
        )
        is expected
    )


def test_evaluate_from_scores_matches_full_evaluate_on_strong_branch() -> None:
    """The score-only path agrees with the full evaluation on the strong
    branch (no failure list needed)."""
    full = evaluate_tampering(
        metadata_score=60, math_score=0, validation_failures=[]
    )
    coarse = evaluate_tampering_from_scores(metadata_score=60, math_score=0)
    assert full.fires is True
    assert coarse is True


# ----------------------------------------------------------------------
# Return shape / source-of-truth contract
# ----------------------------------------------------------------------


def test_rationale_is_human_readable_string() -> None:
    result = evaluate_tampering(
        metadata_score=55, math_score=0, validation_failures=[]
    )
    assert isinstance(result.rationale, str)
    assert result.rationale  # non-empty


def test_evaluation_is_frozen() -> None:
    """The dataclass is frozen — callers can't mutate the evaluation
    after the pipeline returns it. Defensive against an unintended
    mutation cascading into the audit row."""
    result = evaluate_tampering(
        metadata_score=0, math_score=0, validation_failures=[]
    )
    with pytest.raises(AttributeError):
        result.fires = True  # type: ignore[misc]


def test_contributing_failures_is_a_list_copy() -> None:
    """When the medium branch fires, the contributing_failures list is
    only the matching subset — not the whole failures argument. Future
    auditors should see exactly what corroborated."""
    failures = [
        "reconciliation_failed_period",
        "invalid_period: end before start",  # not a corroborator
        "customer_concentration",  # not a corroborator
    ]
    result = evaluate_tampering(
        metadata_score=35, math_score=55, validation_failures=failures
    )
    assert result.fires is True
    assert result.contributing_failures == ["reconciliation_failed_period"]


# ----------------------------------------------------------------------
# Defensive: an empty failures list never fires the medium branch
# ----------------------------------------------------------------------


def test_medium_branch_does_not_fire_without_any_failure() -> None:
    """The medium branch REQUIRES a corroborating failure. With an empty
    failures list it can never fire, regardless of math_score."""
    result = evaluate_tampering(
        metadata_score=35, math_score=100, validation_failures=[]
    )
    assert result.fires is False


def test_non_corroborating_failure_alone_doesnt_fire_medium() -> None:
    """A validation failure that isn't a reconciliation_failed / future_dated
    (e.g. invalid_period) doesn't satisfy the medium branch by itself.
    The score-only re-eval relaxes this (math_score>=55) but the parse-
    time eval is the source of truth."""
    result = evaluate_tampering(
        metadata_score=35,
        math_score=25,
        validation_failures=["invalid_period: 7 days outside 14-50"],
    )
    assert result.fires is False
    assert result.branch == "none"


def _smoke_evaluation_shape(result: TamperingEvaluation) -> None:
    """Cheap shape assertion shared across tests."""
    assert result.metadata_score >= 0
    assert result.math_score >= 0
    assert result.branch in ("strong_metadata", "medium_corroborated", "none")
    assert isinstance(result.contributing_failures, list)


def test_all_paths_return_well_shaped_evaluation() -> None:
    for ms, vs, fs in (
        (60, 0, []),
        (35, 55, ["reconciliation_failed_period"]),
        (0, 0, []),
        (40, 0, []),
    ):
        _smoke_evaluation_shape(
            evaluate_tampering(
                metadata_score=ms, math_score=vs, validation_failures=fs
            )
        )
