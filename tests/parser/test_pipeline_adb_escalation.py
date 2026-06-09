"""R1.7 — ADB partial-coverage escalation (shadow flag).

The aggregator emits ``adb_partial_coverage:{skipped}/{period}`` when its
printed-mode ADB skips one or more days for lack of a closing
running_balance. Pre-R1.7 the pipeline ignored that flag, so an ADB
computed over 60% of the period rendered as a clean number on the
dossier — silently misleading.

R1.7 introduces a deterministic post-aggregation check: if
``skipped / period > 10%`` the pipeline appends a SHADOW evidence flag
(``adb_coverage_thin:...would_route_review``) without changing
``parse_status``. The flag's name explicitly carries ``would_route_review``
so the operator can review the would-be live routing during the corpus
validation window. Live routing flips later via config — not in this
commit.

These tests exercise the helper directly (``_adb_coverage_thin_flag``)
because:
  * the helper is a pure function over the aggregator's flag list, and
  * direct exercise gives boundary-condition coverage (10% / 11% / 9%)
    without rebuilding a full bank-statement fixture per case.

The end-to-end pipeline wiring (the helper actually being called) is
covered indirectly by the existing ``test_pipeline_e2e.py`` clean run +
the explicit NSF e2e tests in ``test_nsf_secondary.py``.
"""

from __future__ import annotations

import pytest

from aegis.parser.pipeline import (
    ADB_COVERAGE_THIN_RATIO_THRESHOLD,
    _adb_coverage_thin_flag,
)


def test_above_threshold_emits_shadow_flag() -> None:
    """5/31 = 16.1% > 10% → shadow flag fires with the rendered percent."""
    flag = _adb_coverage_thin_flag(["adb_partial_coverage:5/31"])
    assert flag is not None
    # 5/31 → 16.13% → round 16
    assert "adb_coverage_thin:" in flag
    assert "skip_ratio=16pct" in flag
    assert "threshold=10pct" in flag
    assert "would_route_review" in flag


def test_below_threshold_does_not_emit_flag() -> None:
    """2/31 = 6.5% < 10% → no flag."""
    assert _adb_coverage_thin_flag(["adb_partial_coverage:2/31"]) is None


def test_no_partial_coverage_flag_does_not_emit() -> None:
    """No partial-coverage flag in the aggregator output → no flag."""
    assert _adb_coverage_thin_flag([]) is None
    assert (
        _adb_coverage_thin_flag(["lender_proceeds_excluded:1_$500.00_(ondeck)"])
        is None
    )
    assert _adb_coverage_thin_flag(["payroll_cadence:biweekly_12%_of_revenue"]) is None


def test_threshold_is_inclusive_10pct_no_flag() -> None:
    """Exactly 10% (3/30) is NOT a violation — the threshold is strictly >.

    Documents the exact-edge behavior so future tuning doesn't drift.
    """
    # 3/30 = 0.10 exactly → at threshold, no flag.
    assert _adb_coverage_thin_flag(["adb_partial_coverage:3/30"]) is None


def test_just_over_threshold_emits() -> None:
    """4/30 = 13.3% > 10% → flag fires."""
    flag = _adb_coverage_thin_flag(["adb_partial_coverage:4/30"])
    assert flag is not None
    assert "skip_ratio=13pct" in flag


def test_zero_skip_does_not_emit() -> None:
    """0/31 (clean coverage tagged for some reason) does not fire."""
    assert _adb_coverage_thin_flag(["adb_partial_coverage:0/31"]) is None


def test_malformed_flag_returns_none() -> None:
    """Defense against future format drift: malformed payloads return None."""
    assert _adb_coverage_thin_flag(["adb_partial_coverage:notanumber/31"]) is None
    assert _adb_coverage_thin_flag(["adb_partial_coverage:5"]) is None
    assert _adb_coverage_thin_flag(["adb_partial_coverage:5/notanumber"]) is None


def test_zero_period_returns_none() -> None:
    """Math undefined on zero denominator — defensively return None.

    Aggregator can't actually emit this today (period_days = max(1, ...)),
    but the helper guards against it.
    """
    assert _adb_coverage_thin_flag(["adb_partial_coverage:5/0"]) is None


def test_processes_first_matching_flag_only() -> None:
    """A duplicate flag is a no-op — first match wins, both produce the same string."""
    out = _adb_coverage_thin_flag(
        [
            "adb_partial_coverage:5/31",
            "adb_partial_coverage:2/31",
        ]
    )
    assert out is not None
    assert "skip_ratio=16pct" in out


def test_threshold_constant_is_decimal_ten_pct() -> None:
    """Pin the threshold so future tuning is a deliberate edit."""
    from decimal import Decimal

    assert ADB_COVERAGE_THIN_RATIO_THRESHOLD == Decimal("0.10")


@pytest.mark.parametrize(
    ("skipped", "period", "expected_pct"),
    [
        (4, 30, 13),  # 13.33%
        (5, 30, 17),  # 16.67% → 17
        (10, 31, 32),  # 32.26%
        (15, 31, 48),  # 48.39%
        (20, 31, 65),  # 64.52% → 65
    ],
)
def test_rendered_percent_matches_calculation(
    skipped: int, period: int, expected_pct: int
) -> None:
    """Spot-check percent rendering across the typical range of skips."""
    flag = _adb_coverage_thin_flag([f"adb_partial_coverage:{skipped}/{period}"])
    assert flag is not None
    assert f"skip_ratio={expected_pct}pct" in flag
