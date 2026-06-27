"""Square transactions CSV extractor tests.

The fixture (``fixtures/square_transactions_minimal.csv``) is a PII-free
synthetic export whose column layout matches Square's documented
transactions CSV format verbatim — see
https://squareup.com/help/article/5161-export-transactions-and-payments
(Square Help Center: "Export Transactions and Payments", verified
2026-06-26).

The 8 rows cover the validator's surface:
  * 5 ``Payment`` rows         → gross_charge kind
  * 2 ``Refund`` rows          → refund kind
  * 1 ``Chargeback`` row       → chargeback kind
The inline ``Fee`` column on Payment rows + on the chargeback row gets
emitted as a synthetic ``fee`` line item so the validator's per-kind
tie-out + the gross-refund-chargeback-fee == payout identity both hold
to the cent. The Square CSV doesn't carry a payout row; the extractor
synthesises one from the identity so the validator stays passing.

Aggregate values are asserted to the cent. ``avg_daily_volume`` is
asserted explicitly so the period_days denominator (March 1 → March 22,
inclusive = 22 days) gets locked into the contract.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aegis.parser.processor.aggregate import aggregate_processor
from aegis.parser.processor.csv_square import (
    SquareCsvError,
    extract_square_csv,
)
from aegis.parser.processor.dossier_aggregates import (
    build_stripe_dossier_aggregates,
)
from aegis.parser.processor.validate import validate_processor

_FIXTURE = Path(__file__).parent / "fixtures" / "square_transactions_minimal.csv"


def _load_fixture() -> bytes:
    return _FIXTURE.read_bytes()


# ---------------------------------------------------------------------------
# Extraction shape
# ---------------------------------------------------------------------------


def test_extract_square_csv_returns_processor_statement() -> None:
    statement = extract_square_csv(_load_fixture(), business_name="Acme Coffee LLC")
    assert statement.summary.processor == "square"
    assert statement.summary.business_name == "Acme Coffee LLC"


def test_period_dates_derived_from_row_stream() -> None:
    """The CSV doesn't have a summary block — period dates are min/max
    of the ``Date`` column."""
    statement = extract_square_csv(_load_fixture())
    assert statement.summary.period_start.isoformat() == "2026-03-01"
    assert statement.summary.period_end.isoformat() == "2026-03-22"


def test_each_row_carries_source_attribution() -> None:
    """Every row must have ``source_page`` + ``source_line``. CSV
    source_page is always 1; source_line is the CSV row number
    (header = line 1)."""
    statement = extract_square_csv(_load_fixture())
    for row in statement.transactions:
        assert row.source_page == 1
        assert row.source_line >= 2  # header occupies line 1


# ---------------------------------------------------------------------------
# Money math — assert to the cent
# ---------------------------------------------------------------------------


def test_aggregates_charges_refunds_chargebacks_fees_payouts() -> None:
    """Tie-out values match the fixture math exactly.

    Payments:  12.50 + 28.75 + 425.00 + 52.40 + 89.25 = 607.90
    Refunds:    5.00 + 18.50                          =  23.50
    Disputes:  75.00                                  =  75.00 (principal)
    Fees:       0.36 + 0.83 + 12.33 + 1.52 + 2.59 (payment fees)
                + 15.00 (chargeback fee)              =  32.63
    Derived payout (synthetic): 607.90 - 23.50 - 75.00 - 32.63 = 476.77

    Identity check: 607.90 - 23.50 - 75.00 - 32.63 = 476.77 == payouts. ✓
    """
    statement = extract_square_csv(_load_fixture())
    agg = aggregate_processor(statement.transactions)

    assert agg.gross_volume.value == Decimal("607.90")
    assert agg.refunds_total.value == Decimal("23.50")
    assert agg.chargebacks_total.value == Decimal("75.00")
    assert agg.fees_total.value == Decimal("32.63")
    assert agg.payouts_total.value == Decimal("476.77")
    # Identity holds within tolerance.
    expected_payouts = (
        agg.gross_volume.value
        - agg.refunds_total.value
        - agg.chargebacks_total.value
        - agg.fees_total.value
    )
    assert abs(expected_payouts - agg.payouts_total.value) < Decimal("0.01")


def test_validator_passes_on_minimal_fixture() -> None:
    """The deterministic gate must accept the fixture — the whole point
    of the CSV path is that the printed totals (summed from rows) tie
    out by construction."""
    statement = extract_square_csv(_load_fixture())
    result = validate_processor(statement)
    assert result.passed is True, f"validation failures: {result.failures}"


def test_avg_daily_volume_uses_period_days_denominator() -> None:
    """avg_daily_volume = gross / period_days (period_days inclusive of
    both endpoints). Locks the denominator into the contract."""
    statement = extract_square_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    dossier = build_stripe_dossier_aggregates(statement, base)
    # March 1 → March 22 inclusive = 22 days.
    assert dossier.period_days == 22
    expected = (Decimal("607.90") / Decimal(22)).quantize(Decimal("0.01"))
    assert dossier.avg_daily_volume == expected


def test_charge_count_excludes_synthetic_payout_row() -> None:
    """The synthetic payout row must not inflate gross_charge counts.
    Five Payment rows in the fixture → charge_count = 5."""
    statement = extract_square_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    assert base.transaction_count.value == 5
    assert base.refund_count.value == 2
    assert base.chargeback_count.value == 1


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_extract_empty_buffer_raises() -> None:
    with pytest.raises(SquareCsvError, match="empty CSV buffer"):
        extract_square_csv(b"")


def test_extract_oversized_buffer_raises() -> None:
    huge = b"a" * (25 * 1024 * 1024 + 1)
    with pytest.raises(SquareCsvError, match="CSV buffer too large"):
        extract_square_csv(huge)


def test_extract_missing_required_columns_raises() -> None:
    """A CSV that's missing the ``Event Type`` column can't discriminate
    kinds — fail closed."""
    bad_csv = (
        b"Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID\n"
        b"2026-03-01,10:00,UTC,Sale,10,0.30,9.70,sq_test\n"
    )
    with pytest.raises(SquareCsvError, match="missing required columns"):
        extract_square_csv(bad_csv)


def test_unknown_event_type_defaults_to_adjustment_not_gross_charge() -> None:
    """CLAUDE.md scoring discipline: unknown event types must NEVER
    default to gross_charge — they'd inflate revenue silently. The
    extractor maps unknown values to ``adjustment`` instead."""
    header = b"Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID,Event Type\n"
    data = (
        b"2026-03-01,10:00,UTC,Mystery transaction,99.99,0.00,99.99,"
        b"sq_mystery_001,SomeNewSquareType\n"
    )
    csv_bytes = header + data
    statement = extract_square_csv(csv_bytes)
    # The one row should land as adjustment, NOT as gross_charge.
    assert all(r.kind != "gross_charge" for r in statement.transactions)
    # And the gross_volume in the summary should be zero.
    assert statement.summary.gross_volume == Decimal("0.00")


def test_us_date_format_accepted_as_fallback() -> None:
    """Older Square exports use MM/DD/YY. The parser accepts both ISO
    and US formats."""
    csv_bytes = (
        b"Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID,Event Type\n"
        b"03/01/26,10:00,UTC,Card sale,12.50,0.36,12.14,sq_test_001,Payment\n"
    )
    statement = extract_square_csv(csv_bytes)
    assert statement.summary.period_start.isoformat() == "2026-03-01"
