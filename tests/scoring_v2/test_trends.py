"""Tests for ``aegis.scoring_v2.trends.compute_revenue_trends``.

Coverage per the spec:

* growing / flat / declining bands at clear interior points
* boundary cases at exactly +10% (inclusive growth) and exactly -10%
  (inclusive decline)
* zero-anchor handling (non-zero latest → growing, zero latest → flat)
* fallback paths for empty list and single-bucket input
* NSF placeholder pin so a silent flip is caught when the field lands
* many-month input — only the last two buckets participate
"""

from __future__ import annotations

from decimal import Decimal

from aegis.scoring.models import MonthBreakdown
from aegis.scoring_v2.trends import (
    DECLINING_THRESHOLD_PCT,
    GROWING_THRESHOLD_PCT,
    RevenueTrends,
    compute_revenue_trends,
)


def _bucket(
    month: str,
    deposits: Decimal,
    avg_balance: Decimal,
    withdrawals: Decimal = Decimal("0.00"),
    nsf_count: int = 0,
) -> MonthBreakdown:
    """Build a ``MonthBreakdown`` with sensible defaults for the
    fields the trends function doesn't read."""
    return MonthBreakdown(
        month=month,
        deposits=deposits,
        withdrawals=withdrawals,
        avg_balance=avg_balance,
        nsf_count=nsf_count,
    )


# ─────────────────────────────────────────────────────────────────────
# Case 1 — Growing (+15%)
# ─────────────────────────────────────────────────────────────────────


def test_growing_at_plus_15_percent_returns_growing() -> None:
    """Anchor 10000 → latest 11500 = +15%. Above the +10 band."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00")),
        _bucket("2026-02", Decimal("11500.00"), Decimal("5750.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert isinstance(result, RevenueTrends)
    assert result.revenue_trend == "growing"
    assert result.adb_trend == "growing"
    assert result.months_compared == 2


# ─────────────────────────────────────────────────────────────────────
# Case 2 — Flat (+5%, inside the band)
# ─────────────────────────────────────────────────────────────────────


def test_flat_at_plus_5_percent_returns_flat() -> None:
    """Anchor 10000 → latest 10500 = +5%. Inside the ±10 band."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00")),
        _bucket("2026-02", Decimal("10500.00"), Decimal("5250.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "flat"
    assert result.adb_trend == "flat"
    assert result.months_compared == 2


# ─────────────────────────────────────────────────────────────────────
# Case 3 — Declining (-20%)
# ─────────────────────────────────────────────────────────────────────


def test_declining_at_minus_20_percent_returns_declining() -> None:
    """Anchor 10000 → latest 8000 = -20%. Below the -10 band."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00")),
        _bucket("2026-02", Decimal("8000.00"), Decimal("4000.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "declining"
    assert result.adb_trend == "declining"
    assert result.months_compared == 2


# ─────────────────────────────────────────────────────────────────────
# Case 4 — Single month → all flat, months_compared == 1
# ─────────────────────────────────────────────────────────────────────


def test_single_month_falls_back_to_all_flat() -> None:
    """One bucket isn't enough to compute a direction."""
    buckets = [_bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"))]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "flat"
    assert result.adb_trend == "flat"
    assert result.nsf_trend == "flat"
    assert result.months_compared == 1


# ─────────────────────────────────────────────────────────────────────
# Case 5 — Exactly +10% (inclusive boundary → growing)
# ─────────────────────────────────────────────────────────────────────


def test_exactly_plus_10_percent_is_growing_inclusive() -> None:
    """The +10 boundary belongs to ``growing``."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00")),
        _bucket("2026-02", Decimal("11000.00"), Decimal("5500.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "growing"
    assert result.adb_trend == "growing"
    assert GROWING_THRESHOLD_PCT == Decimal("10")


# ─────────────────────────────────────────────────────────────────────
# Case 6 — Exactly -10% (inclusive boundary → declining)
# ─────────────────────────────────────────────────────────────────────


def test_exactly_minus_10_percent_is_declining_inclusive() -> None:
    """The -10 boundary belongs to ``declining``."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00")),
        _bucket("2026-02", Decimal("9000.00"), Decimal("4500.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "declining"
    assert result.adb_trend == "declining"
    assert DECLINING_THRESHOLD_PCT == Decimal("-10")


# ─────────────────────────────────────────────────────────────────────
# Case 7 — Zero anchor, non-zero latest → growing
# ─────────────────────────────────────────────────────────────────────


def test_zero_anchor_with_nonzero_latest_is_growing() -> None:
    """Any movement off a zero baseline counts as growth."""
    buckets = [
        _bucket("2026-01", Decimal("0.00"), Decimal("0.00")),
        _bucket("2026-02", Decimal("5000.00"), Decimal("2500.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "growing"
    assert result.adb_trend == "growing"


# ─────────────────────────────────────────────────────────────────────
# Case 8 — Zero anchor, zero latest → flat
# ─────────────────────────────────────────────────────────────────────


def test_zero_anchor_with_zero_latest_is_flat() -> None:
    """No movement off zero is flat, not declining."""
    buckets = [
        _bucket("2026-01", Decimal("0.00"), Decimal("0.00")),
        _bucket("2026-02", Decimal("0.00"), Decimal("0.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "flat"
    assert result.adb_trend == "flat"


# ─────────────────────────────────────────────────────────────────────
# Case 9 — Empty list → all flat, months_compared == 0
# ─────────────────────────────────────────────────────────────────────


def test_empty_list_returns_all_flat_without_raising() -> None:
    """No buckets at all is a legitimate input — the parser may
    produce zero monthly rollups on a sub-month statement."""
    result = compute_revenue_trends([])
    assert result.revenue_trend == "flat"
    assert result.adb_trend == "flat"
    assert result.nsf_trend == "flat"
    assert result.months_compared == 0


# ─────────────────────────────────────────────────────────────────────
# Case 10 — NSF trend reads MonthBreakdown.nsf_count (Sprint 4 unblock)
# ─────────────────────────────────────────────────────────────────────


def test_nsf_trend_growing_from_zero_to_one() -> None:
    """0 → 1 is meaningful — first NSF surfaces. ``growing``."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"), nsf_count=0),
        _bucket("2026-02", Decimal("10000.00"), Decimal("5000.00"), nsf_count=1),
    ]
    assert compute_revenue_trends(buckets).nsf_trend == "growing"


def test_nsf_trend_declining_from_some_to_zero() -> None:
    """N → 0 is the merchant cleaning up. ``declining``."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"), nsf_count=3),
        _bucket("2026-02", Decimal("10000.00"), Decimal("5000.00"), nsf_count=0),
    ]
    assert compute_revenue_trends(buckets).nsf_trend == "declining"


def test_nsf_trend_flat_both_zero() -> None:
    """0 → 0 stays flat — the clean operating norm shouldn't read as
    a trend either way."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"), nsf_count=0),
        _bucket("2026-02", Decimal("10000.00"), Decimal("5000.00"), nsf_count=0),
    ]
    assert compute_revenue_trends(buckets).nsf_trend == "flat"


def test_nsf_trend_uses_percentage_band_above_one() -> None:
    """Above 1, the same ±10% / count-based band applies.
    4 → 5 = +25% → ``growing``; 5 → 4 = -20% → ``declining``."""
    growing = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"), nsf_count=4),
        _bucket("2026-02", Decimal("10000.00"), Decimal("5000.00"), nsf_count=5),
    ]
    declining = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"), nsf_count=5),
        _bucket("2026-02", Decimal("10000.00"), Decimal("5000.00"), nsf_count=4),
    ]
    assert compute_revenue_trends(growing).nsf_trend == "growing"
    assert compute_revenue_trends(declining).nsf_trend == "declining"


def test_nsf_trend_flat_when_change_below_threshold() -> None:
    """10 → 11 is +10% (on the inclusive boundary → growing); 10 → 10
    is flat. Use 10 → 10 here and 5 → 5 with the no-change reading."""
    buckets = [
        _bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"), nsf_count=10),
        _bucket("2026-02", Decimal("10000.00"), Decimal("5000.00"), nsf_count=10),
    ]
    assert compute_revenue_trends(buckets).nsf_trend == "flat"


def test_nsf_trend_flat_on_single_or_empty_input() -> None:
    """Same fallback as revenue / ADB: < 2 buckets → ``flat``."""
    one_bucket = [_bucket("2026-01", Decimal("10000.00"), Decimal("5000.00"), nsf_count=3)]
    assert compute_revenue_trends(one_bucket).nsf_trend == "flat"
    assert compute_revenue_trends([]).nsf_trend == "flat"


# ─────────────────────────────────────────────────────────────────────
# Case 11 — Many months → only the last two participate
# ─────────────────────────────────────────────────────────────────────


def test_many_months_only_last_two_participate_in_comparison() -> None:
    """Six months of history; the trend is computed from months 5 + 6
    only. Months 1-4 must not pull the result toward themselves —
    regression guard against a future change that averages the
    trailing window without re-thinking the threshold."""
    buckets = [
        _bucket("2026-01", Decimal("1000.00"), Decimal("500.00")),
        _bucket("2026-02", Decimal("2000.00"), Decimal("1000.00")),
        _bucket("2026-03", Decimal("3000.00"), Decimal("1500.00")),
        _bucket("2026-04", Decimal("4000.00"), Decimal("2000.00")),
        _bucket("2026-05", Decimal("10000.00"), Decimal("5000.00")),
        _bucket("2026-06", Decimal("8000.00"), Decimal("4000.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "declining"
    assert result.adb_trend == "declining"
    assert result.months_compared == 6


def test_unsorted_input_is_normalized_before_comparison() -> None:
    """Callers don't have to pre-sort — the function sorts ascending
    by ``month`` string before picking anchor + latest."""
    buckets = [
        _bucket("2026-06", Decimal("8000.00"), Decimal("4000.00")),
        _bucket("2026-01", Decimal("1000.00"), Decimal("500.00")),
        _bucket("2026-05", Decimal("10000.00"), Decimal("5000.00")),
    ]
    result = compute_revenue_trends(buckets)
    assert result.revenue_trend == "declining"
    assert result.adb_trend == "declining"


def test_trends_model_has_no_decline_or_score_field() -> None:
    """Mirror the Track A / B / C + mca_stack + balance_health
    structural guard. Shadow-only by design."""
    fields = set(RevenueTrends.model_fields)
    forbidden = {
        "decline",
        "auto_decline",
        "risk_score",
        "fraud_score",
        "score",
        "outcome",
        "hard_decline_reasons",
    }
    leaked = fields & forbidden
    assert not leaked, f"RevenueTrends must not carry decline/score fields; leaked: {leaked}"
