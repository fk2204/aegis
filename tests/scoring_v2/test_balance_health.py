"""Tests for ``aegis.scoring_v2.balance_health.compute_balance_health``.

Coverage per the spec the operator handed over:

* zero transactions
* all positive days
* exactly 8 negative days (no trigger — strict ``>`` on the
  ``NEGATIVE_DAYS_TRAILING_3M_THRESHOLD`` gate)
* 9 negative days (trigger fires)
* ADB exactly at 5% (no trigger — strict ``<`` on the
  ``LOW_ADB_PCT_THRESHOLD`` gate)
* ADB below 5% (trigger fires)
* mixed period with different trailing-3m vs full-period counts

Plus structural guards: source ids typed as UUID, schema has no
decline field, monthly deposits == 0 yields None pct, ``period_days``
clamps to 1 on zero.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.balance_health import (
    LOW_ADB_PCT_THRESHOLD,
    NEGATIVE_DAYS_TRAILING_3M_THRESHOLD,
    BalanceHealthAggregation,
    compute_balance_health,
)


def _deposit(
    amount: Decimal,
    posted_date: date,
    *,
    running_balance: Decimal | None = None,
    page: int = 1,
    line: int = 1,
) -> ClassifiedTransaction:
    """Build a classified ``deposit`` row. Amount is stored as
    positive (deposits credit the account)."""
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description="CUSTOMER DEPOSIT",
        amount=amount,
        source_page=page,
        source_line=line,
        category="deposit",
        classification_confidence=95,
        running_balance=running_balance,
    )


def _withdrawal(
    amount: Decimal,
    posted_date: date,
    *,
    running_balance: Decimal | None = None,
    page: int = 1,
    line: int = 1,
) -> ClassifiedTransaction:
    """Build a non-deposit debit (category ``other``). Amount is
    stored as the negative magnitude. Pass a positive ``amount`` —
    the helper applies the sign."""
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description="GENERAL WITHDRAWAL",
        amount=-amount,
        source_page=page,
        source_line=line,
        category="other",
        classification_confidence=95,
        running_balance=running_balance,
    )


# ─────────────────────────────────────────────────────────────────────
# Case 1 — zero transactions
# ─────────────────────────────────────────────────────────────────────


def test_zero_transactions_clean_zeros() -> None:
    """No transactions → all zeros, no triggers, no lowest-balance date."""
    agg = compute_balance_health(transactions=[], period_days=30)
    assert isinstance(agg, BalanceHealthAggregation)
    assert agg.avg_daily_balance == Decimal("0.00")
    assert agg.avg_daily_balance_source_ids == ()
    assert agg.adb_as_pct_of_monthly_deposits is None
    assert agg.adb_as_pct_of_monthly_deposits_source_ids == ()
    assert agg.negative_days == 0
    assert agg.negative_days_source_ids == ()
    assert agg.negative_days_trailing_3m == 0
    assert agg.negative_days_trailing_3m_source_ids == ()
    assert agg.lowest_balance == Decimal("0.00")
    assert agg.lowest_balance_date is None
    assert agg.lowest_balance_source_ids == ()
    assert agg.shadow_triggers == ()


# ─────────────────────────────────────────────────────────────────────
# Case 2 — all positive days
# ─────────────────────────────────────────────────────────────────────


def test_all_positive_days_no_triggers() -> None:
    """30-day window, deposits on day 1 + day 30. No negative days,
    high ADB-to-deposits ratio. No triggers."""
    transactions = [
        _deposit(Decimal("1000.00"), date(2026, 1, 1)),
        _deposit(Decimal("500.00"), date(2026, 1, 30), line=2),
    ]
    agg = compute_balance_health(transactions=transactions, period_days=30)
    # Closings: day 1 = 1000, days 2-29 = 1000 carry, day 30 = 1500.
    # Sum = 1000 * 29 + 1500 = 30500. ADB = 30500 / 30 ≈ 1016.67.
    assert agg.avg_daily_balance == Decimal("1016.67")
    assert agg.negative_days == 0
    assert agg.negative_days_trailing_3m == 0
    # Lowest closing is 1000 across days 1-29; earliest-date tie-break
    # → day 1 wins.
    assert agg.lowest_balance == Decimal("1000.00")
    assert agg.lowest_balance_date == date(2026, 1, 1)
    assert agg.shadow_triggers == ()


# ─────────────────────────────────────────────────────────────────────
# Case 3 — exactly 8 negative days (no trigger — strict ``>``)
# ─────────────────────────────────────────────────────────────────────


def test_exactly_8_negative_days_does_not_trigger() -> None:
    """Strict ``>`` on the gate — exactly 8 is the boundary that
    does NOT fire."""
    txns: list[ClassifiedTransaction] = [
        _deposit(Decimal("1000.00"), date(2026, 1, 1)),
        # Day 2 close = -100 (negative day 1 of 8)
        _withdrawal(Decimal("1100.00"), date(2026, 1, 2), line=2),
    ]
    # Days 3-9 each subtract $1 → closings -101 to -107 (7 more neg days = 8 total)
    for i, day_idx in enumerate(range(3, 10), start=3):
        txns.append(_withdrawal(Decimal("1.00"), date(2026, 1, day_idx), line=10 + i))
    # Day 10 rescue → close = -107 + 500 = 393. Days 11-30 carry 393.
    txns.append(_deposit(Decimal("500.00"), date(2026, 1, 10), line=20))
    # Anchor period_end on day 30 so the function derives a 30-day window
    # back to day 1 (matches period_days=30).
    txns.append(_deposit(Decimal("0.01"), date(2026, 1, 30), line=21))

    agg = compute_balance_health(transactions=txns, period_days=30)
    assert agg.negative_days == 8
    assert agg.negative_days_trailing_3m == 8
    assert NEGATIVE_DAYS_TRAILING_3M_THRESHOLD == 8
    # Strict ``>`` → 8 does NOT trigger.
    assert all(not t.startswith("negative_days_shadow") for t in agg.shadow_triggers)


# ─────────────────────────────────────────────────────────────────────
# Case 4 — 9 negative days (trigger fires)
# ─────────────────────────────────────────────────────────────────────


def test_9_negative_days_fires_trigger() -> None:
    """One extra negative day past the boundary → trigger fires."""
    txns: list[ClassifiedTransaction] = [
        _deposit(Decimal("1000.00"), date(2026, 1, 1)),
        _withdrawal(Decimal("1100.00"), date(2026, 1, 2), line=2),
    ]
    # Days 3-10 each subtract $1 → 8 more negative days (total 9).
    for i, day_idx in enumerate(range(3, 11), start=3):
        txns.append(_withdrawal(Decimal("1.00"), date(2026, 1, day_idx), line=10 + i))
    # Day 11 rescue.
    txns.append(_deposit(Decimal("1000.00"), date(2026, 1, 11), line=20))
    txns.append(_deposit(Decimal("0.01"), date(2026, 1, 30), line=21))

    agg = compute_balance_health(transactions=txns, period_days=30)
    assert agg.negative_days == 9
    assert agg.negative_days_trailing_3m == 9
    matching = [t for t in agg.shadow_triggers if t.startswith("negative_days_shadow")]
    assert len(matching) == 1
    assert matching[0] == "negative_days_shadow:9"


# ─────────────────────────────────────────────────────────────────────
# Case 5 — ADB exactly at 5% (no trigger — strict ``<``)
# ─────────────────────────────────────────────────────────────────────


def test_adb_pct_exactly_at_5_does_not_trigger() -> None:
    """Strict ``<`` on the gate — exactly 5% is the boundary that
    does NOT fire. Day 1 pins close = 1000 via ``running_balance``;
    days 2-29 carry; day 30 anchor preserves the close. Result:
    ADB = 1000, deposit_total = 20000 → avg_monthly_deposits = 20000
    → 5.00% exactly.
    """
    txns = [
        # Day 1: deposit amount + printed running_balance = 1000.
        # The function snaps close to 1000 here; days 2-29 carry it.
        _deposit(
            Decimal("20000.00"),
            date(2026, 1, 1),
            running_balance=Decimal("1000.00"),
        ),
        # Day 30 anchors ``period_end``. Zero-amount, non-deposit
        # category so it neither moves the close nor affects
        # gross-deposit totals.
        _withdrawal(Decimal("0.00"), date(2026, 1, 30), line=2),
    ]
    agg = compute_balance_health(transactions=txns, period_days=30)
    assert agg.avg_daily_balance == Decimal("1000.00")
    # avg_monthly_deposits = 20000 / 30 * 30 = 20000.
    # pct = 1000 / 20000 * 100 = 5.00.
    assert agg.adb_as_pct_of_monthly_deposits == Decimal("5.00")
    assert agg.adb_as_pct_of_monthly_deposits == LOW_ADB_PCT_THRESHOLD
    # Strict ``<`` → 5.00 does NOT trigger.
    assert all(not t.startswith("low_adb_shadow") for t in agg.shadow_triggers)


# ─────────────────────────────────────────────────────────────────────
# Case 6 — ADB below 5% (trigger fires)
# ─────────────────────────────────────────────────────────────────────


def test_adb_pct_below_5_fires_low_adb_trigger() -> None:
    """ADB / monthly deposits = 4.95% → low_adb_shadow fires."""
    txns = [
        # Same anchoring pattern as the 5%-exact test, just with a
        # ``running_balance`` of 990 instead of 1000.
        _deposit(
            Decimal("20000.00"),
            date(2026, 1, 1),
            running_balance=Decimal("990.00"),
        ),
        _withdrawal(Decimal("0.00"), date(2026, 1, 30), line=2),
    ]
    agg = compute_balance_health(transactions=txns, period_days=30)
    assert agg.avg_daily_balance == Decimal("990.00")
    # avg_monthly_deposits = 20000. pct = 990 / 20000 * 100 = 4.95.
    assert agg.adb_as_pct_of_monthly_deposits == Decimal("4.95")
    matching = [t for t in agg.shadow_triggers if t.startswith("low_adb_shadow")]
    assert len(matching) == 1
    assert matching[0] == "low_adb_shadow:4.95%"


# ─────────────────────────────────────────────────────────────────────
# Case 7 — mixed period: trailing-3m count differs from full-period
# ─────────────────────────────────────────────────────────────────────


def test_mixed_period_trailing_3m_differs_from_full_period_count() -> None:
    """180-day window with negative days CLUSTERED in days 1-30 (outside
    the trailing 90-day window). Trailing count is 0, full count is 30.
    Demonstrates the trailing-3m gate's specificity: a deal whose
    cashflow stress is OLD doesn't fire the trigger, only one whose
    stress is RECENT does."""
    txns: list[ClassifiedTransaction] = [
        # Day 1 starts with withdrawal — beginning_balance derived as 0
        # (no running_balance on first row).
        _withdrawal(Decimal("100.00"), date(2026, 1, 1)),
    ]
    # Days 2-30 each subtract $1 → 29 more negative days (total 30).
    for day_idx in range(2, 31):
        txns.append(
            _withdrawal(
                Decimal("1.00"),
                date(2026, 1, 1) + timedelta(days=day_idx - 1),
                line=day_idx,
            )
        )
    # Day 31 rescue deposit +200 → close = -129 + 200 = 71. Days 32-180
    # carry 71 (positive).
    txns.append(_deposit(Decimal("200.00"), date(2026, 1, 31), line=100))
    # Anchor period_end on day 180 (2026-06-29).
    period_end = date(2026, 1, 1) + timedelta(days=179)
    txns.append(_deposit(Decimal("0.01"), period_end, line=101))

    agg = compute_balance_health(transactions=txns, period_days=180)
    assert agg.negative_days == 30
    # Trailing window = last 90 days (day 91-180). All negative days
    # are in days 1-30 → trailing count is 0.
    assert agg.negative_days_trailing_3m == 0
    # Neither trigger fires: trailing-3m count (0) is below the
    # ``> 8`` gate, and ADB at ~40 against ~33 monthly deposits is
    # well above 5%.
    assert agg.shadow_triggers == ()
    # Trailing-3m source ids must also be empty.
    assert agg.negative_days_trailing_3m_source_ids == ()
    # But the full-period negative-day sources do exist (30 days
    # with at least one txn each).
    assert len(agg.negative_days_source_ids) >= 30


# ─────────────────────────────────────────────────────────────────────
# Structural guards
# ─────────────────────────────────────────────────────────────────────


def test_source_ids_are_uuid_typed_per_audit_rule() -> None:
    """Every source-id tuple carries real UUIDs."""
    txns = [
        _deposit(Decimal("1000.00"), date(2026, 1, 1)),
        _withdrawal(Decimal("200.00"), date(2026, 1, 5), line=2),
    ]
    agg = compute_balance_health(transactions=txns, period_days=30)
    assert all(isinstance(uid, UUID) for uid in agg.avg_daily_balance_source_ids)
    assert all(isinstance(uid, UUID) for uid in agg.adb_as_pct_of_monthly_deposits_source_ids)
    assert all(isinstance(uid, UUID) for uid in agg.negative_days_source_ids)
    assert all(isinstance(uid, UUID) for uid in agg.negative_days_trailing_3m_source_ids)
    assert all(isinstance(uid, UUID) for uid in agg.lowest_balance_source_ids)


def test_zero_monthly_deposits_yields_none_pct() -> None:
    """Only-withdrawals window → gross deposits == 0 → pct is None.
    No low_adb_shadow trigger because the gate requires a defined pct.
    """
    txns = [
        _withdrawal(Decimal("100.00"), date(2026, 1, 1)),
        _withdrawal(Decimal("50.00"), date(2026, 1, 5), line=2),
    ]
    agg = compute_balance_health(transactions=txns, period_days=30)
    assert agg.adb_as_pct_of_monthly_deposits is None
    assert agg.adb_as_pct_of_monthly_deposits_source_ids == ()
    assert all(not t.startswith("low_adb_shadow") for t in agg.shadow_triggers)


def test_period_days_zero_is_clamped_to_one() -> None:
    """Edge case: empty observation window. Treat as 1 day, no crash."""
    txns = [_deposit(Decimal("100.00"), date(2026, 1, 1))]
    agg = compute_balance_health(transactions=txns, period_days=0)
    # Single-day window → close = 100, ADB = 100.
    assert agg.avg_daily_balance == Decimal("100.00")
    assert agg.lowest_balance == Decimal("100.00")
    assert agg.lowest_balance_date == date(2026, 1, 1)


def test_lowest_balance_picks_the_minimum_day_with_source_ids() -> None:
    """Three different closings → lowest is the most-negative day; its
    source ids include every row posted that day."""
    txns = [
        _deposit(Decimal("500.00"), date(2026, 1, 1)),
        _withdrawal(Decimal("700.00"), date(2026, 1, 2), line=2),  # close -200
        _deposit(Decimal("1000.00"), date(2026, 1, 3), line=3),  # close +800
    ]
    agg = compute_balance_health(transactions=txns, period_days=10)
    assert agg.lowest_balance == Decimal("-200.00")
    assert agg.lowest_balance_date == date(2026, 1, 2)
    assert len(agg.lowest_balance_source_ids) == 1


def test_aggregation_has_no_decline_or_score_field() -> None:
    """Mirror the Track A / B / C + mca_stack structural guard.
    Shadow-only by design."""
    fields = set(BalanceHealthAggregation.model_fields)
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
    assert not leaked, (
        f"BalanceHealthAggregation must not carry decline/score fields; leaked: {leaked}"
    )
