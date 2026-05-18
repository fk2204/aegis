"""Processor aggregates: source-attribution invariants (mp Phase 6.6).

Every aggregate carries the row UUIDs that produced it. The dossier's
"where did this number come from?" drill-down relies on this — same
discipline as the bank parser's ``_source_ids`` fields.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aegis.parser.processor.aggregate import aggregate_processor
from aegis.parser.processor.models import ProcessorLineItem


def _row(*, kind: str, amount: str) -> ProcessorLineItem:
    return ProcessorLineItem(
        posted_date=date(2026, 1, 15),
        description=f"{kind}-row",
        kind=kind,
        amount=Decimal(amount),
        source_page=2,
        source_line=1,
    )


def test_aggregate_groups_by_kind() -> None:
    rows = [
        _row(kind="gross_charge", amount="500.00"),
        _row(kind="gross_charge", amount="300.00"),
        _row(kind="refund", amount="50.00"),
        _row(kind="chargeback", amount="20.00"),
        _row(kind="fee", amount="30.00"),
        _row(kind="payout", amount="700.00"),
    ]
    agg = aggregate_processor(rows)
    assert agg.gross_volume.value == Decimal("800.00")
    assert agg.refunds_total.value == Decimal("50.00")
    assert agg.chargebacks_total.value == Decimal("20.00")
    assert agg.fees_total.value == Decimal("30.00")
    assert agg.payouts_total.value == Decimal("700.00")
    assert agg.net_revenue.value == Decimal("700.00")  # 800 - 50 - 20 - 30
    assert agg.transaction_count.value == 2
    assert agg.refund_count.value == 1
    assert agg.chargeback_count.value == 1


def test_aggregate_source_ids_match_input_rows() -> None:
    """Every aggregate's source_ids must reference the line items that
    produced it. Without this, the drill-down is a lie."""
    rows = [
        _row(kind="gross_charge", amount="500.00"),
        _row(kind="refund", amount="50.00"),
    ]
    agg = aggregate_processor(rows)
    assert agg.gross_volume.source_ids == [rows[0].id]
    assert agg.refunds_total.source_ids == [rows[1].id]
    # net_revenue's source_ids span every contributing kind.
    assert rows[0].id in agg.net_revenue.source_ids
    assert rows[1].id in agg.net_revenue.source_ids


def test_chargeback_ratio_zero_when_no_gross() -> None:
    """No charges in the period → ratio = 0 (not a div-by-zero crash)."""
    rows = [_row(kind="fee", amount="5.00")]
    agg = aggregate_processor(rows)
    assert agg.chargeback_ratio == Decimal("0")


def test_chargeback_ratio_computed_correctly() -> None:
    rows = [
        _row(kind="gross_charge", amount="10000.00"),
        _row(kind="chargeback", amount="150.00"),
    ]
    agg = aggregate_processor(rows)
    assert agg.chargeback_ratio == Decimal("150.00") / Decimal("10000.00")


def test_adjustments_excluded_from_aggregates() -> None:
    """Adjustments are deliberately outside the validator's identity;
    they shouldn't show up in any aggregate either. Otherwise a small
    balance correction would poison gross_volume."""
    rows = [
        _row(kind="gross_charge", amount="1000.00"),
        _row(kind="adjustment", amount="50.00"),
        _row(kind="payout", amount="1000.00"),
    ]
    agg = aggregate_processor(rows)
    assert agg.gross_volume.value == Decimal("1000.00")
    # No "adjustment" surface anywhere on ``agg`` — it's intentionally absent.
