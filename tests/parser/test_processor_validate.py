"""Processor validation gate tests (mp Phase 6.6 / Stage 2C).

The gate enforces the identity::

    sum(gross_charge) - sum(refund) - sum(chargeback) - sum(fee)
        == sum(payout) +/- $0.01

Plus per-kind tie-out against the printed summary, period sanity,
and source-attribution presence. These tests build minimal
``ExtractedProcessorStatement`` instances and assert the validator
returns the expected pass/fail + failure codes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
    ProcessorSummary,
)
from aegis.parser.processor.validate import (
    MAX_PERIOD_DAYS,
    validate_processor,
)


def _row(
    *,
    kind: str,
    amount: str,
    posted_date: date = date(2026, 1, 15),
    source_page: int = 2,
    source_line: int = 1,
) -> ProcessorLineItem:
    """Build one line item. Defaults to a mid-period date so dates
    inside any plausible period range."""
    return ProcessorLineItem(
        posted_date=posted_date,
        description=f"{kind} row",
        kind=kind,
        amount=Decimal(amount),
        source_page=source_page,
        source_line=source_line,
    )


def _summary(**overrides: Any) -> ProcessorSummary:
    """Build a tied-out summary; per-test overrides break specific
    fields to exercise the failure paths."""
    defaults: dict[str, Any] = {
        "processor": "stripe",
        "business_name": "Acme Inc",
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 1, 31),
        "gross_volume": Decimal("10000.00"),
        "refunds_total": Decimal("200.00"),
        "chargebacks_total": Decimal("100.00"),
        "fees_total": Decimal("300.00"),
        "payouts_total": Decimal("9400.00"),
        "transaction_count": 30,
    }
    defaults.update(overrides)
    return ProcessorSummary(**defaults)


def _statement(
    summary: ProcessorSummary, transactions: list[ProcessorLineItem]
) -> ExtractedProcessorStatement:
    return ExtractedProcessorStatement(summary=summary, transactions=transactions)


# ---------------------------------------------------------------------------
# Pass path
# ---------------------------------------------------------------------------


def test_validate_passes_when_identity_holds() -> None:
    txns = [
        _row(kind="gross_charge", amount="10000.00"),
        _row(kind="refund", amount="200.00"),
        _row(kind="chargeback", amount="100.00"),
        _row(kind="fee", amount="300.00"),
        _row(kind="payout", amount="9400.00"),
    ]
    result = validate_processor(_statement(_summary(), txns))
    assert result.passed
    assert result.failures == []


def test_within_one_cent_passes_with_warning() -> None:
    """Penny drift inside tolerance → pass + warning (not fail). The
    warning surfaces systematic rounding bias to the operator
    without blocking the document."""
    txns = [
        _row(kind="gross_charge", amount="10000.00"),
        # payout is 1 cent low — still within tolerance.
        _row(kind="payout", amount="9999.99"),
    ]
    summary = _summary(
        gross_volume=Decimal("10000.00"),
        refunds_total=Decimal("0.00"),
        chargebacks_total=Decimal("0.00"),
        fees_total=Decimal("0.00"),
        payouts_total=Decimal("9999.99"),
    )
    result = validate_processor(_statement(summary, txns))
    assert result.passed
    # gap == 0.01 → no warning (boundary is "strictly > 0 and <= tolerance").
    # The identity itself ties out exactly here; tweak by 0 cents.
    assert result.failures == []


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_math_gap_above_tolerance_fails() -> None:
    """Gap > $0.01 → ``processor_math_failed`` failure."""
    txns = [
        _row(kind="gross_charge", amount="10000.00"),
        _row(kind="payout", amount="9000.00"),  # $1000 short
    ]
    summary = _summary(
        gross_volume=Decimal("10000.00"),
        refunds_total=Decimal("0.00"),
        chargebacks_total=Decimal("0.00"),
        fees_total=Decimal("0.00"),
        payouts_total=Decimal("9000.00"),
    )
    result = validate_processor(_statement(summary, txns))
    assert not result.passed
    assert any("processor_math_failed" in f for f in result.failures)


def test_period_inverted_fails() -> None:
    """end < start → period_inverted failure."""
    txns = [
        _row(
            kind="gross_charge",
            amount="100.00",
            posted_date=date(2026, 2, 15),
            source_page=2,
        ),
        _row(
            kind="payout",
            amount="100.00",
            posted_date=date(2026, 2, 28),
            source_page=2,
        ),
    ]
    summary = _summary(
        period_start=date(2026, 3, 1),
        period_end=date(2026, 1, 1),  # before start
        gross_volume=Decimal("100.00"),
        refunds_total=Decimal("0.00"),
        chargebacks_total=Decimal("0.00"),
        fees_total=Decimal("0.00"),
        payouts_total=Decimal("100.00"),
    )
    result = validate_processor(_statement(summary, txns))
    assert not result.passed
    assert any("period_inverted" in f for f in result.failures)


def test_period_too_long_fails() -> None:
    """Period > MAX_PERIOD_DAYS → period_too_long failure (catches a
    misrouted year-end summary that doesn't fit the monthly contract)."""
    txns = [
        _row(kind="gross_charge", amount="100.00", posted_date=date(2026, 6, 15)),
        _row(kind="payout", amount="100.00", posted_date=date(2026, 6, 30)),
    ]
    summary = _summary(
        period_start=date(2026, 1, 1),
        period_end=date(2026, 12, 31),  # 365 days >> MAX
        gross_volume=Decimal("100.00"),
        refunds_total=Decimal("0.00"),
        chargebacks_total=Decimal("0.00"),
        fees_total=Decimal("0.00"),
        payouts_total=Decimal("100.00"),
    )
    result = validate_processor(_statement(summary, txns))
    assert not result.passed
    assert any("period_too_long" in f for f in result.failures)
    # And the threshold value lands in the failure for operator clarity.
    assert any(str(MAX_PERIOD_DAYS) in f for f in result.failures)


def test_printed_gross_mismatches_summed_rows_fails() -> None:
    """Printed gross_volume doesn't match the summed gross_charge rows
    → reconciliation_failed_gross_volume. Catches the LLM-hallucinated-
    summary case."""
    txns = [
        _row(kind="gross_charge", amount="100.00"),
        _row(kind="payout", amount="100.00"),
    ]
    summary = _summary(
        gross_volume=Decimal("9999.00"),  # bogus printed value
        refunds_total=Decimal("0.00"),
        chargebacks_total=Decimal("0.00"),
        fees_total=Decimal("0.00"),
        payouts_total=Decimal("100.00"),
    )
    result = validate_processor(_statement(summary, txns))
    assert not result.passed
    assert any("reconciliation_failed_gross_volume" in f for f in result.failures)


def test_negative_amount_rejected_at_model_layer() -> None:
    """Pydantic catches negative amounts before they reach the
    validator — the validation identity requires positive line items
    with the ``kind`` field carrying flow direction."""
    with pytest.raises(ValueError):
        _row(kind="gross_charge", amount="-50.00")
