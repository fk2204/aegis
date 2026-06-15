"""Tests for ``aegis.scoring_v2.mca_stack.aggregate_mca_stack``.

Coverage per the spec the operator handed over:

* zero MCA debits → clean pass, no triggers, no lender label
* single MCA counterparty
* multiple MCAs below either threshold
* combined holdback exactly at 50% → no overload trigger (strict ``>``)
* combined holdback above 50% → overload trigger fires
* active count = 3 → no count trigger
* active count = 4 → count trigger fires

Plus structural guards: source ids populated per AEGIS audit-trail
rule, largest-single picks the right counterparty, deterministic
tie-break.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.mca_stack import (
    MCA_STACK_COUNT_THRESHOLD,
    MCA_STACK_OVERLOADED_PCT,
    MCAStackAggregation,
    aggregate_mca_stack,
)


def _mca_debit(
    description: str,
    amount: Decimal,
    *,
    posted_date: date = date(2026, 2, 14),
    page: int = 1,
    line: int = 1,
) -> ClassifiedTransaction:
    """Build a classified mca_debit row for the test fixtures.

    ``amount`` is stored as the signed magnitude (debits are negative,
    matching the parser convention). Pass a positive number — the
    helper applies the sign.
    """
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=-amount,
        source_page=page,
        source_line=line,
        category="mca_debit",
        classification_confidence=95,
    )


def _deposit(
    description: str,
    amount: Decimal,
    *,
    posted_date: date = date(2026, 2, 14),
    page: int = 1,
    line: int = 1,
) -> ClassifiedTransaction:
    """Build a classified deposit row — should be ignored by the aggregator."""
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        source_page=page,
        source_line=line,
        category="deposit",
        classification_confidence=95,
    )


# ─────────────────────────────────────────────────────────────────────
# Case 1 — zero MCA debits (clean pass)
# ─────────────────────────────────────────────────────────────────────


def test_zero_mca_debits_clean_pass() -> None:
    """Non-MCA transactions are ignored; result is empty but well-typed."""
    transactions = [
        _deposit("CUSTOMER PAYMENT", Decimal("12000.00")),
        _deposit("PAYROLL CREDIT", Decimal("500.00")),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("25000.00"),
        period_days=30,
    )
    assert isinstance(agg, MCAStackAggregation)
    assert agg.active_mca_count == 0
    assert agg.active_mca_source_ids == ()
    assert agg.mca_monthly_load == Decimal("0.00")
    assert agg.mca_monthly_load_source_ids == ()
    assert agg.estimated_combined_holdback_pct is None
    assert agg.largest_single_mca_monthly == Decimal("0.00")
    assert agg.largest_single_mca_lender is None
    assert agg.largest_single_mca_source_ids == ()
    assert agg.shadow_triggers == ()


# ─────────────────────────────────────────────────────────────────────
# Case 2 — single MCA counterparty
# ─────────────────────────────────────────────────────────────────────


def test_single_mca_counterparty() -> None:
    """One lender, several debits. Largest == combined; one source-id set."""
    transactions = [
        _mca_debit("KAPITUS DEBIT 12345", Decimal("100.00"), line=1),
        _mca_debit("KAPITUS DEBIT 12346", Decimal("100.00"), line=2),
        _mca_debit("KAPITUS DEBIT 12347", Decimal("100.00"), line=3),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("30000.00"),
        period_days=30,
    )
    assert agg.active_mca_count == 1
    # total $300 / 30 days * 22 business days = $220.00
    assert agg.mca_monthly_load == Decimal("220.00")
    assert agg.largest_single_mca_monthly == Decimal("220.00")
    assert agg.largest_single_mca_lender == "KAPITUS"
    assert len(agg.active_mca_source_ids) == 3
    assert agg.active_mca_source_ids == agg.mca_monthly_load_source_ids
    assert agg.active_mca_source_ids == agg.largest_single_mca_source_ids
    # 220 / 30000 * 100 = 0.7333...% -> 0.73 after .quantize
    assert agg.estimated_combined_holdback_pct == Decimal("0.73")
    assert agg.shadow_triggers == ()


# ─────────────────────────────────────────────────────────────────────
# Case 3 — multiple MCAs below either threshold
# ─────────────────────────────────────────────────────────────────────


def test_multiple_mcas_below_both_thresholds() -> None:
    """Two lenders, modest burden — no triggers."""
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("80.00"), line=1),
        _mca_debit("KAPITUS DEBIT", Decimal("80.00"), line=2),
        _mca_debit("ON DECK ACH PMT", Decimal("60.00"), line=3),
        _mca_debit("ON DECK ACH PMT", Decimal("60.00"), line=4),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("40000.00"),
        period_days=30,
    )
    assert agg.active_mca_count == 2
    # ($160 + $120) / 30 * 22 = $205.33
    assert agg.mca_monthly_load == Decimal("205.33")
    # Largest is KAPITUS at $160 / 30 * 22 = $117.33
    assert agg.largest_single_mca_lender == "KAPITUS"
    assert agg.largest_single_mca_monthly == Decimal("117.33")
    assert agg.shadow_triggers == ()


# ─────────────────────────────────────────────────────────────────────
# Case 4 — combined holdback exactly at 50%
# ─────────────────────────────────────────────────────────────────────


def test_holdback_exactly_at_50_pct_does_not_trigger() -> None:
    """Strict ``>`` on the overload gate — exactly 50% is the boundary."""
    # Pick debit totals so monthly_load / monthly_revenue * 100 == 50.00
    # exactly. monthly_load = 5000.00 vs monthly_revenue = 10000.00.
    # To hit monthly_load 5000.00: total / period_days * 22 = 5000.00.
    # For period_days=22: total / 22 * 22 = total → total = 5000.00.
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("5000.00"), line=1),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("10000.00"),
        period_days=22,
    )
    assert agg.estimated_combined_holdback_pct == Decimal("50.00")
    assert agg.estimated_combined_holdback_pct == MCA_STACK_OVERLOADED_PCT
    # Strict > → 50.00 does NOT trigger.
    assert all(not t.startswith("mca_stack_overloaded_shadow") for t in agg.shadow_triggers)


# ─────────────────────────────────────────────────────────────────────
# Case 5 — combined holdback above 50% → overload trigger fires
# ─────────────────────────────────────────────────────────────────────


def test_holdback_above_50_pct_fires_overload_trigger() -> None:
    """One cent past 50% → mca_stack_overloaded_shadow fires."""
    # monthly_load $5001.00 vs revenue $10000.00 → 50.01%
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("5001.00"), line=1),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("10000.00"),
        period_days=22,
    )
    assert agg.estimated_combined_holdback_pct == Decimal("50.01")
    matching = [t for t in agg.shadow_triggers if t.startswith("mca_stack_overloaded_shadow")]
    assert len(matching) == 1
    assert matching[0] == "mca_stack_overloaded_shadow:50.01%"


# ─────────────────────────────────────────────────────────────────────
# Case 6 — active count = 3 → no count trigger
# ─────────────────────────────────────────────────────────────────────


def test_active_count_3_does_not_fire_count_trigger() -> None:
    """``>=`` on the count gate — 3 is below the 4 threshold."""
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("50.00"), line=1),
        _mca_debit("ON DECK ACH", Decimal("50.00"), line=2),
        _mca_debit("RAPID FINANCE", Decimal("50.00"), line=3),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("50000.00"),
        period_days=30,
    )
    assert agg.active_mca_count == 3
    assert MCA_STACK_COUNT_THRESHOLD == 4  # guards the spec
    assert all(not t.startswith("mca_stack_count_shadow") for t in agg.shadow_triggers)


# ─────────────────────────────────────────────────────────────────────
# Case 7 — active count = 4 → count trigger fires
# ─────────────────────────────────────────────────────────────────────


def test_active_count_4_fires_count_trigger() -> None:
    """``>=`` on the count gate — 4 is at the boundary and fires."""
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("50.00"), line=1),
        _mca_debit("ON DECK ACH", Decimal("50.00"), line=2),
        _mca_debit("RAPID FINANCE", Decimal("50.00"), line=3),
        _mca_debit("LIBERTAS FUNDING", Decimal("50.00"), line=4),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("50000.00"),
        period_days=30,
    )
    assert agg.active_mca_count == 4
    matching = [t for t in agg.shadow_triggers if t.startswith("mca_stack_count_shadow")]
    assert len(matching) == 1
    assert matching[0] == "mca_stack_count_shadow:4"


# ─────────────────────────────────────────────────────────────────────
# Structural guards
# ─────────────────────────────────────────────────────────────────────


def test_largest_single_picks_the_right_counterparty() -> None:
    """Three lenders with distinct totals → largest is the top spender."""
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("100.00"), line=1),
        _mca_debit("ON DECK", Decimal("400.00"), line=2),
        _mca_debit("ON DECK", Decimal("400.00"), line=3),
        _mca_debit("RAPID FINANCE", Decimal("50.00"), line=4),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("100000.00"),
        period_days=30,
    )
    assert agg.active_mca_count == 3
    assert agg.largest_single_mca_lender == "ON DECK"
    # ON DECK total = 800 / 30 * 22 = 586.67
    assert agg.largest_single_mca_monthly == Decimal("586.67")
    assert len(agg.largest_single_mca_source_ids) == 2


def test_source_ids_are_uuid_typed_per_audit_rule() -> None:
    """Every source-id tuple must carry real UUIDs, never strings or None."""
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("100.00"), line=1),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("30000.00"),
        period_days=30,
    )
    assert all(isinstance(uid, UUID) for uid in agg.active_mca_source_ids)
    assert all(isinstance(uid, UUID) for uid in agg.mca_monthly_load_source_ids)
    assert all(isinstance(uid, UUID) for uid in agg.largest_single_mca_source_ids)


def test_revenue_zero_yields_none_holdback_pct() -> None:
    """``monthly_revenue == 0`` → None (no divide-by-zero, no 0% lie)."""
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("100.00"), line=1),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("0"),
        period_days=30,
    )
    assert agg.estimated_combined_holdback_pct is None
    # Overload trigger needs a defined pct to fire — must not appear.
    assert all(not t.startswith("mca_stack_overloaded_shadow") for t in agg.shadow_triggers)


def test_period_days_zero_is_clamped_to_one() -> None:
    """Edge case: empty observation window. Treat as 1 day, no crash."""
    transactions = [
        _mca_debit("KAPITUS DEBIT", Decimal("100.00"), line=1),
    ]
    agg = aggregate_mca_stack(
        transactions=transactions,
        monthly_revenue=Decimal("10000.00"),
        period_days=0,
    )
    # 100 / 1 * 22 = 2200.00
    assert agg.mca_monthly_load == Decimal("2200.00")


def test_aggregation_has_no_decline_or_score_field() -> None:
    """Mirror the Track A / B / C structural guard. Shadow-only by design."""
    fields = set(MCAStackAggregation.model_fields)
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
    assert not leaked, f"MCAStackAggregation must not carry decline/score fields; leaked: {leaked}"
