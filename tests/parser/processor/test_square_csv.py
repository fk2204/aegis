"""Square transactions CSV extractor tests.

The fixture (``fixtures/square_sample.csv``) is a synthetic-but-realistic
CSV whose column layout and Event Type vocabulary mirror a real Square
Dashboard transactions export. Per CLAUDE.md "External-integration test
discipline", tests for code that ingests an external system's payload
should ideally validate against a CAPTURED REAL response. A real
sanitised Square export is the long-term target; see
``fixtures/README.md`` § "Replacement procedure".

The synthetic fixture is tolerated here because:
- Column header set is byte-for-byte the documented Square format
  (all 15 columns present, not just the structural-signature subset).
- Event Type values exercise every branch of ``_SQUARE_EVENT_TYPE_MAP``
  (``Payment`` → gross_charge, ``Refund`` → refund, ``Chargeback`` →
  chargeback, unknown / ``Transfer`` / ``Adjustment`` → adjustment).
- Fee math matches Square's standard 2.6% + $0.10 rate.
- The synthetic-payout derivation path is exercised (the fixture has no
  payout row of its own, so ``extract_square_csv`` derives one from the
  identity).

The 15 rows cover the validator's surface:
  * 10 ``Payment`` rows         → gross_charge kind (+ 10 synthetic fee items)
  * 2  ``Refund`` rows          → refund kind
  * 1  ``Chargeback`` row       → chargeback kind (+ 1 synthetic fee item)
  * 1  ``Transfer`` row         → adjustment kind (excluded from identity)
  * 1  ``Adjustment`` row       → adjustment kind (excluded from identity)
The inline ``Fee`` column on Payment rows + on the chargeback row gets
emitted as a synthetic ``fee`` line item so the validator's per-kind
tie-out + the gross - refund - chargeback - fee == payout identity both
hold to the cent. The Square CSV doesn't carry a real payout row; the
extractor synthesises one from the identity so the validator stays
passing.
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

_FIXTURE = Path(__file__).parent / "fixtures" / "square_sample.csv"


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
    assert statement.summary.period_start.isoformat() == "2026-04-02"
    assert statement.summary.period_end.isoformat() == "2026-04-30"


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

    Payments:  12.50 + 487.00 + 28.75 + 67.80 + 89.25 + 145.50
             + 215.00 + 32.10 + 15.75 + 52.40                = 1146.05
    Refunds:    8.50 + 32.10                                 =   40.60
    Chargebacks: 145.50                                      =  145.50
    Fees: payment fees (0.43 + 12.76 + 0.85 + 1.86 + 2.42
                       + 3.88 + 5.69 + 0.93 + 0.51 + 1.46) = 30.79
        + chargeback fee (15.00)                             =   45.79
    Derived payout (synthetic): 1146.05 - 40.60 - 145.50 - 45.79 = 914.16

    Identity check: 1146.05 - 40.60 - 145.50 - 45.79 = 914.16 == payouts. ✓
    """
    statement = extract_square_csv(_load_fixture())
    agg = aggregate_processor(statement.transactions)

    assert agg.gross_volume.value == Decimal("1146.05")
    assert agg.refunds_total.value == Decimal("40.60")
    assert agg.chargebacks_total.value == Decimal("145.50")
    assert agg.fees_total.value == Decimal("45.79")
    assert agg.payouts_total.value == Decimal("914.16")
    # Identity holds within tolerance.
    expected_payouts = (
        agg.gross_volume.value
        - agg.refunds_total.value
        - agg.chargebacks_total.value
        - agg.fees_total.value
    )
    assert abs(expected_payouts - agg.payouts_total.value) < Decimal("0.01")


def test_validator_passes_on_sample_fixture() -> None:
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
    # April 2 → April 30 inclusive = 29 days.
    assert dossier.period_days == 29
    expected = (Decimal("1146.05") / Decimal(29)).quantize(Decimal("0.01"))
    assert dossier.avg_daily_volume == expected


def test_charge_count_excludes_synthetic_payout_row() -> None:
    """The synthetic payout row must not inflate gross_charge counts.
    Ten Payment rows in the fixture → charge_count = 10."""
    statement = extract_square_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    assert base.transaction_count.value == 10
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
