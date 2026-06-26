"""Stripe balance-transactions CSV extractor tests.

The fixture (``fixtures/stripe_balance_transactions_minimal.csv``) is a
PII-free synthetic export whose column layout matches Stripe's documented
balance-transactions CSV format verbatim — see
https://docs.stripe.com/reports/balance-transaction-types and
https://support.stripe.com/questions/how-to-customize-balance-transaction-reports.

The 10 rows cover the full validator surface:
  * 5 ``charge`` rows  (Stripe Type=charge)        — gross_charge kind
  * 2 ``refund`` rows  (Stripe Type=refund)        — refund kind
  * 1 ``dispute`` row  (Stripe Type=dispute)       — chargeback kind
  * 2 ``payout`` rows  (Stripe Type=payout)        — payout kind
                                                     (NEGATIVE Amount —
                                                     Stripe ledger sign
                                                     convention)
The inline ``Fee`` column on the 5 charges and on the dispute row gets
emitted as synthetic ``fee`` line items so the validator's per-kind
tie-out and the gross-refund-chargeback-fee == payout identity both
hold to the cent.

Aggregate values are asserted to the cent. ``avg_daily_volume`` is
asserted explicitly so the period_days denominator (March 1 → March 25,
inclusive = 25 days) gets locked into the contract.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aegis.parser.processor.aggregate import aggregate_processor
from aegis.parser.processor.csv_stripe import (
    StripeCsvError,
    extract_stripe_csv,
)
from aegis.parser.processor.dossier_aggregates import (
    build_stripe_dossier_aggregates,
)
from aegis.parser.processor.validate import validate_processor

_FIXTURE = Path(__file__).parent / "fixtures" / "stripe_balance_transactions_minimal.csv"


def _load_fixture() -> bytes:
    return _FIXTURE.read_bytes()


# ---------------------------------------------------------------------------
# Extraction shape
# ---------------------------------------------------------------------------


def test_extract_stripe_csv_returns_processor_statement() -> None:
    """The CSV path produces the same shape as the PDF path —
    ``ExtractedProcessorStatement`` with ``processor="stripe"``."""
    statement = extract_stripe_csv(_load_fixture(), business_name="Acme Tech LLC")
    assert statement.summary.processor == "stripe"
    assert statement.summary.business_name == "Acme Tech LLC"


def test_period_dates_derived_from_row_stream() -> None:
    """The CSV doesn't have a summary block — period dates are min/max
    of the ``Created (UTC)`` column."""
    statement = extract_stripe_csv(_load_fixture())
    # First row: 2026-03-01; last row: 2026-03-25.
    assert statement.summary.period_start.isoformat() == "2026-03-01"
    assert statement.summary.period_end.isoformat() == "2026-03-25"


def test_each_row_carries_source_attribution() -> None:
    """Every row must have ``source_page`` + ``source_line`` so the
    audit drill-down resolves. CSV ``source_page`` is always 1 and
    ``source_line`` is the CSV row number (header = line 1)."""
    statement = extract_stripe_csv(_load_fixture())
    for row in statement.transactions:
        assert row.source_page == 1
        assert row.source_line >= 2  # header occupies line 1


# ---------------------------------------------------------------------------
# Money math — assert to the cent
# ---------------------------------------------------------------------------


def test_aggregates_charges_refunds_chargebacks_fees_payouts() -> None:
    """Tie-out values match the fixture math exactly.

    Charges:  500 + 1250 + 800 + 650 + 1100 = 4300.00
    Refunds:   75 + 50                       =  125.00
    Disputes:  200                           =  200.00 (principal)
    Fees:     14.80 + 36.55 + 23.50 + 19.15 + 32.20 (charge fees)
              + 15.00 (dispute fee)          =  141.20
    Payouts: 2459.45 + 1374.35               = 3833.80

    Identity check: 4300 - 125 - 200 - 141.20 = 3833.80 == payouts. ✓
    """
    statement = extract_stripe_csv(_load_fixture())
    agg = aggregate_processor(statement.transactions)
    assert agg.gross_volume.value == Decimal("4300.00")
    assert agg.refunds_total.value == Decimal("125.00")
    assert agg.chargebacks_total.value == Decimal("200.00")
    assert agg.fees_total.value == Decimal("141.20")
    assert agg.payouts_total.value == Decimal("3833.80")
    # net_revenue = gross - refunds - chargebacks - fees
    assert agg.net_revenue.value == Decimal("3833.80")


def test_validator_passes_on_csv_extraction() -> None:
    """The synthesized summary (built from summed rows) trivially ties
    out per-kind. The identity check + period sanity also pass."""
    statement = extract_stripe_csv(_load_fixture())
    result = validate_processor(statement)
    assert result.passed, result.failures


def test_dossier_aggregates_include_avg_daily_volume_and_counts() -> None:
    """Dossier aggregates carry the operator-facing extras: avg daily
    volume, payout count, refund rate, period_days. The denominator
    for avg_daily_volume is (period_end - period_start).days + 1 —
    inclusive of both endpoints."""
    statement = extract_stripe_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    agg = build_stripe_dossier_aggregates(statement, base)
    # March 1 → March 25 inclusive = 25 days.
    assert agg.period_days == 25
    # 4300 / 25 = 172.00 exactly.
    assert agg.avg_daily_volume == Decimal("172.00")
    assert agg.payout_count == 2
    assert agg.charge_count == 5
    assert agg.refund_count == 2
    assert agg.chargeback_count == 1
    # refund_rate = 2 / 5 = 0.4
    assert agg.refund_rate == Decimal("2") / Decimal("5")


def test_dossier_aggregates_source_ids_preserved() -> None:
    """Every dossier aggregate's source_ids reference real line items —
    the drill-down contract."""
    statement = extract_stripe_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    agg = build_stripe_dossier_aggregates(statement, base)
    txn_ids = {t.id for t in statement.transactions}

    for sourced in (
        agg.total_gross_volume,
        agg.total_fees,
        agg.total_net_volume,
        agg.total_payouts,
    ):
        for source_id in sourced.source_ids:
            assert source_id in txn_ids
        assert len(sourced.source_ids) > 0


# ---------------------------------------------------------------------------
# Sign convention — Stripe payouts are NEGATIVE in the balance ledger
# ---------------------------------------------------------------------------


def test_payout_ledger_sign_is_handled() -> None:
    """Stripe's balance-transaction ledger signs payouts NEGATIVE
    (money leaving Stripe → merchant bank). Our ``ProcessorLineItem``
    stores ``amount`` as non-negative and rides flow direction on
    ``kind``. Confirm the CSV extractor took absolute values so a
    payout row's stored amount is positive."""
    statement = extract_stripe_csv(_load_fixture())
    payouts = [r for r in statement.transactions if r.kind == "payout"]
    assert payouts, "fixture must have payout rows"
    for r in payouts:
        assert r.amount > Decimal("0")


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_extract_rejects_empty_buffer() -> None:
    with pytest.raises(StripeCsvError, match="empty"):
        extract_stripe_csv(b"")


def test_extract_rejects_missing_required_columns() -> None:
    """A CSV missing one of the required columns (id / Type / Amount /
    Created) fails closed — we don't try to invent the missing
    discriminator."""
    bad = b"id,Amount,Created (UTC)\ntxn_001,500.00,2026-03-01 10:14:00\n"
    with pytest.raises(StripeCsvError, match="missing required columns"):
        extract_stripe_csv(bad)


def test_extract_handles_utf8_bom() -> None:
    """Stripe Dashboard writes UTF-8 with BOM. The first column name
    must come back clean (no ``﻿id``)."""
    fixture = _load_fixture()
    with_bom = b"\xef\xbb\xbf" + fixture
    statement = extract_stripe_csv(with_bom)
    # If the BOM leaked into the column name, the required-column check
    # would have failed above. Verify we got transactions.
    assert len(statement.transactions) > 0


def test_extract_rejects_header_only_csv() -> None:
    header_only = (
        b"id,Type,Source,Amount,Fee,Net,Currency,Created (UTC),Available On (UTC),Description\n"
    )
    with pytest.raises(StripeCsvError, match="no data rows"):
        extract_stripe_csv(header_only)


def test_unknown_type_defaults_to_adjustment() -> None:
    """A Stripe Type we don't recognize defaults to ``"adjustment"`` so
    it's excluded from the validation identity. Silently mapping an
    unknown type to ``gross_charge`` could let a misclassified row
    inflate revenue."""
    novel = (
        b"id,Type,Source,Amount,Fee,Net,Currency,Created (UTC),"
        b"Available On (UTC),Description\n"
        b"txn_new,future_type_we_dont_know,n/a,99.99,0,99.99,usd,"
        b"2026-03-15 10:00:00,2026-03-17 00:00:00,Mystery row\n"
    )
    statement = extract_stripe_csv(novel)
    assert len(statement.transactions) == 1
    assert statement.transactions[0].kind == "adjustment"
