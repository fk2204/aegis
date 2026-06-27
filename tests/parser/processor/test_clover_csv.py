"""Clover transactions CSV extractor tests.

The fixture (``fixtures/clover_sample.csv``) is a synthetic-but-realistic
CSV whose column layout and discriminating values mirror a real Clover
Dashboard transactions export. Per CLAUDE.md "External-integration test
discipline", tests for code that ingests an external system's payload
should ideally validate against a CAPTURED REAL response. A real
sanitised Clover export is the long-term target; see
``fixtures/README.md`` § "Replacement procedure".

The synthetic fixture is tolerated here because:
- Column header set is byte-for-byte the documented Clover format (all
  15 columns present).
- The kind-mapping branches all exercise (Payment / Refund / Void /
  Adjustment / negative-amount fallback).
- Cash payment row exercises the empty-Card-Type branch (cash IS
  revenue and must still land as ``gross_charge``).
- Synthetic payout derivation path is exercised (the fixture has no
  payout row of its own, so ``extract_clover_csv`` derives one from
  the identity).

The 15 rows cover the validator's surface:
  * 10 Payment rows  → gross_charge (Sale descriptions, varied card types)
  * 1  cash Payment  → gross_charge (Payment Type = Cash, empty card info)
  * 2  Refund rows   → refund (Description contains "Refund")
  * 1  Void row      → adjustment (Auth Code = VOID; excluded from identity)
  * 1  Adjustment    → adjustment (Description contains "Adjustment")
The Clover CSV doesn't carry a real payout row; the extractor
synthesises one from the identity ``gross - refund - chargeback ==
payout`` (Clover has no processor fees, so the simplified identity
applies).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aegis.parser.processor.aggregate import aggregate_processor
from aegis.parser.processor.csv_clover import (
    CloverCsvError,
    extract_clover_csv,
)
from aegis.parser.processor.detect import (
    detect_processor_from_csv_header,
    detect_processor_from_filename,
)
from aegis.parser.processor.dossier_aggregates import (
    build_stripe_dossier_aggregates,
)
from aegis.parser.processor.validate import validate_processor

_FIXTURE = Path(__file__).parent / "fixtures" / "clover_sample.csv"


def _load_fixture() -> bytes:
    return _FIXTURE.read_bytes()


# ---------------------------------------------------------------------------
# Detection — filename + header
# ---------------------------------------------------------------------------


def test_filename_routes_clover() -> None:
    assert detect_processor_from_filename("clover_transactions_2026-05.csv") == "clover"
    assert detect_processor_from_filename("clover-export-2026.csv") == "clover"
    assert detect_processor_from_filename("CLOVER_TRANSACTIONS.CSV") == "clover"


def test_filename_does_not_collide_with_stripe_or_square() -> None:
    """Stripe and Square filename hits must still route to their
    respective brands — a generic ``transactions_<date>.csv`` from
    Stripe (``balance_transactions_<date>.csv``) should NOT be matched
    by the Clover token set."""
    assert detect_processor_from_filename("balance_transactions_2026-05.csv") == "stripe"
    assert detect_processor_from_filename("square_transactions_2026.csv") == "square"


def test_header_sniff_routes_clover() -> None:
    """Header containing both ``Auth Code`` AND ``Device ID`` is the
    Clover-unique discriminator."""
    header = (
        "Date & Time,Description,Amount,Tip,Tax,Total,Payment Type,"
        "Card Type,Last 4,Auth Code,Card Holder Name,Employee,Order ID,"
        "Device ID,Note"
    )
    assert detect_processor_from_csv_header(header) == "clover"


def test_header_sniff_rejects_non_clover_headers() -> None:
    """Stripe / Square headers don't carry ``Auth Code`` — must NOT
    classify as Clover."""
    square_header = (
        "Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID,"
        "Payment ID,Card Brand,PAN Suffix,Device Name,Notes,Event Type,"
        "Location"
    )
    stripe_header = "id,Type,Source,Amount,Currency,Available On,Created,Description,Fee,Net"
    assert detect_processor_from_csv_header(square_header) == "square"
    assert detect_processor_from_csv_header(stripe_header) == "stripe"


# ---------------------------------------------------------------------------
# Extraction shape
# ---------------------------------------------------------------------------


def test_extract_clover_csv_returns_processor_statement() -> None:
    statement = extract_clover_csv(_load_fixture(), business_name="Acme Storefront LLC")
    assert statement.summary.processor == "clover"
    assert statement.summary.business_name == "Acme Storefront LLC"


def test_period_dates_derived_from_row_stream() -> None:
    """The CSV doesn't carry a summary block — period dates are min/max
    of the ``Date & Time`` column."""
    statement = extract_clover_csv(_load_fixture())
    assert statement.summary.period_start.isoformat() == "2026-05-02"
    assert statement.summary.period_end.isoformat() == "2026-05-30"


def test_each_row_carries_source_attribution() -> None:
    """Every row must have ``source_page`` + ``source_line``. CSV
    source_page is always 1; source_line is the CSV row number (header
    occupies line 1)."""
    statement = extract_clover_csv(_load_fixture())
    for row in statement.transactions:
        assert row.source_page == 1
        assert row.source_line >= 2


# ---------------------------------------------------------------------------
# Money math — assert to the cent
# ---------------------------------------------------------------------------


def test_aggregates_charges_refunds_void_adjustment_payout() -> None:
    """Tie-out values match the fixture math exactly.

    11 gross_charge rows (10 card payments + 1 cash):
      4.50 + 12.50 + 25.00 + 45.00 + 87.50 + 98.25 + 145.00 + 156.75
      + 175.00 + 200.00 + 215.00 = 1164.50
    2 refund rows: 12.50 + 25.00 = 37.50
    0 chargebacks (Clover doesn't separate chargebacks in this export)
    0 fees (Clover doesn't report fees in this export)
    Void (87.50) → adjustment → excluded from identity
    Manual Adjustment (5.00) → adjustment → excluded from identity
    Derived payout (synthetic): 1164.50 - 37.50 - 0 - 0 = 1127.00

    Identity: gross - refund - chargeback - fee == payout
              1164.50 - 37.50 - 0 - 0 = 1127.00 == payouts ✓
    """
    statement = extract_clover_csv(_load_fixture())
    agg = aggregate_processor(statement.transactions)

    assert agg.gross_volume.value == Decimal("1164.50")
    assert agg.refunds_total.value == Decimal("37.50")
    assert agg.chargebacks_total.value == Decimal("0.00")
    assert agg.fees_total.value == Decimal("0.00")
    assert agg.payouts_total.value == Decimal("1127.00")
    expected_payouts = (
        agg.gross_volume.value
        - agg.refunds_total.value
        - agg.chargebacks_total.value
        - agg.fees_total.value
    )
    assert abs(expected_payouts - agg.payouts_total.value) < Decimal("0.01")


def test_validator_passes_on_sample_fixture() -> None:
    """The deterministic gate must accept the fixture — the printed
    totals (summed from rows) tie out by construction."""
    statement = extract_clover_csv(_load_fixture())
    result = validate_processor(statement)
    assert result.passed is True, f"validation failures: {result.failures}"


def test_avg_daily_volume_uses_period_days_denominator() -> None:
    """avg_daily_volume = gross / period_days (period_days inclusive of
    both endpoints). Locks the denominator into the contract."""
    statement = extract_clover_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    dossier = build_stripe_dossier_aggregates(statement, base)
    # May 2 → May 30 inclusive = 29 days.
    assert dossier.period_days == 29
    expected = (Decimal("1164.50") / Decimal(29)).quantize(Decimal("0.01"))
    assert dossier.avg_daily_volume == expected


def test_charge_count_excludes_synthetic_payout_row() -> None:
    """The synthetic payout row must not inflate gross_charge counts.
    11 gross_charge rows in the fixture → charge_count = 11.
    Voids and adjustments map to ``adjustment`` (excluded from
    transaction_count which counts gross_charge only)."""
    statement = extract_clover_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    assert base.transaction_count.value == 11
    assert base.refund_count.value == 2
    assert base.chargeback_count.value == 0


# ---------------------------------------------------------------------------
# Kind discrimination
# ---------------------------------------------------------------------------


def test_void_row_maps_to_adjustment_not_refund() -> None:
    """Per CLAUDE.md scoring discipline: a Clover Void typically pairs
    with a Payment row that's also in the export. Mapping to ``refund``
    would net the same dollars out twice; ``adjustment`` keeps it out
    of the identity."""
    statement = extract_clover_csv(_load_fixture())
    void_rows = [
        r
        for r in statement.transactions
        if "Void" in r.description or "void" in r.description.lower()
    ]
    assert len(void_rows) == 1
    assert void_rows[0].kind == "adjustment"


def test_cash_payment_still_lands_as_gross_charge() -> None:
    """Cash payments arrive with empty Card Type / Auth Code; they must
    still count as ``gross_charge`` (cash IS revenue)."""
    statement = extract_clover_csv(_load_fixture())
    # The cash row in the fixture is the 4.50 sale on 2026-05-02.
    cash_candidates = [
        r
        for r in statement.transactions
        if r.amount == Decimal("4.50") and r.kind == "gross_charge"
    ]
    assert len(cash_candidates) == 1


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_extract_empty_buffer_raises() -> None:
    with pytest.raises(CloverCsvError, match="empty CSV buffer"):
        extract_clover_csv(b"")


def test_extract_missing_required_columns_raises() -> None:
    """A Clover CSV missing ``Auth Code`` can't discriminate voids —
    fail closed."""
    bad_csv = b"Date & Time,Description,Amount,Device ID\n2026-05-01 10:00:00,Sale,10.00,device_X\n"
    with pytest.raises(CloverCsvError, match="missing required columns"):
        extract_clover_csv(bad_csv)
