"""Toast sales-export CSV extractor tests.

The fixture (``fixtures/toast_sample.csv``) is a synthetic-but-realistic
CSV whose column layout mirrors a real Toast Reports → Export. Same
discipline as ``test_square_csv.py``: the long-term target is a
captured real export from the operator, but the synthetic fixture is
honest about its column shape and exercises every branch of
``_TOAST_TRANSACTION_TYPE_MAP`` plus the void-handling path.

20 rows breakdown after extraction:
  * 14 ``Payment`` rows (Void=No, Card)   → gross_charge kind
  *  1 ``Payment`` (Cash)                  → gross_charge kind
  *  3 ``Refund`` rows                     → refund kind
  *  2 ``Payment`` (Void=Yes)              → adjustment kind (excluded from identity)

Toast doesn't report processor fees, so ``fees_total`` is $0.00 and
no synthetic fee rows are emitted. The extractor synthesises a payout
from the identity ``gross - refund - chargeback - fee`` so the
validator's tie-out math holds — same posture as the Square extractor.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from aegis.parser.processor.aggregate import aggregate_processor
from aegis.parser.processor.csv_toast import (
    ToastCsvError,
    extract_toast_csv,
)
from aegis.parser.processor.detect import (
    detect_processor_from_csv_header,
    detect_processor_from_filename,
)
from aegis.parser.processor.dossier_aggregates import (
    build_stripe_dossier_aggregates,
)
from aegis.parser.processor.validate import validate_processor

_FIXTURE = Path(__file__).parent / "fixtures" / "toast_sample.csv"


def _load_fixture() -> bytes:
    return _FIXTURE.read_bytes()


# ---------------------------------------------------------------------------
# Detection — filename + CSV header sniff
# ---------------------------------------------------------------------------


def test_detect_filename_positive_toast_variants() -> None:
    """Operator-renamed Toast exports route via filename tokens."""
    assert detect_processor_from_filename("toast_sales_2026-05.csv") == "toast"
    assert detect_processor_from_filename("Toast-Export-May.csv") == "toast"
    assert detect_processor_from_filename("toast_export_q2.csv") == "toast"


def test_detect_filename_negative_other_processors() -> None:
    """Stripe / Square / bank filenames don't false-positive as Toast."""
    assert detect_processor_from_filename("balance_transactions_2026-05.csv") == "stripe"
    assert detect_processor_from_filename("square-transactions-may.csv") == "square"
    assert detect_processor_from_filename("chase-statement.pdf") == "bank"
    # Bare "toast" without the structural words must NOT match Toast —
    # a merchant called "Toast Cafe Inc." shouldn't accidentally route.
    assert detect_processor_from_filename("toast cafe inc statements.pdf") == "bank"


def test_detect_csv_header_positive_real_toast_header() -> None:
    """Toast header with Revenue Center + Dining Options → toast brand."""
    header = (
        "Date,Server,Order ID,Order #,Location,Revenue Center,Tab Name,Item,Qty,"
        "Gross Amount,Discount Amount,Net Amount,Void,Void Reason,Check Amount,"
        "Tip Amount,Total Amount,Transaction Type,Payment Type,Last 4,Card Brand,"
        "Card Holder,Dining Options"
    )
    assert detect_processor_from_csv_header(header) == "toast"


def test_detect_csv_header_negative_stripe_square_bank() -> None:
    """Other processors' headers do NOT match Toast."""
    stripe_header = "id,Type,Source,Amount,Fee,Net,Created (UTC)"
    square_header = "Date,Time,Time Zone,Description,Amount,Fee,Net,Transaction ID"
    bank_header = "Date,Description,Amount,Balance"
    assert detect_processor_from_csv_header(stripe_header) == "stripe"
    assert detect_processor_from_csv_header(square_header) == "square"
    assert detect_processor_from_csv_header(bank_header) == "bank"


# ---------------------------------------------------------------------------
# Extraction shape
# ---------------------------------------------------------------------------


def test_extract_toast_csv_returns_processor_statement() -> None:
    statement = extract_toast_csv(_load_fixture(), business_name="Acme Restaurants LLC")
    assert statement.summary.processor == "toast"
    assert statement.summary.business_name == "Acme Restaurants LLC"


def test_period_dates_derived_from_row_stream() -> None:
    """Toast CSV has no summary block — period dates are min/max of Date."""
    statement = extract_toast_csv(_load_fixture())
    assert statement.summary.period_start.isoformat() == "2026-05-01"
    assert statement.summary.period_end.isoformat() == "2026-05-30"


def test_each_row_carries_source_attribution() -> None:
    """source_page=1 for all CSV rows; source_line is CSV row number (header=1)."""
    statement = extract_toast_csv(_load_fixture())
    for row in statement.transactions:
        assert row.source_page == 1
        assert row.source_line >= 2


# ---------------------------------------------------------------------------
# Kind mapping — payment / cash / refund / void
# ---------------------------------------------------------------------------


def test_payment_and_cash_both_map_to_gross_charge() -> None:
    """Card payments and cash payments are both revenue from the merchant's
    perspective — both map to gross_charge."""
    statement = extract_toast_csv(_load_fixture())
    gross_rows = [r for r in statement.transactions if r.kind == "gross_charge"]
    # 14 card payments + 1 cash = 15 gross_charge rows in the fixture.
    assert len(gross_rows) == 15


def test_refund_rows_map_to_refund_kind() -> None:
    """Transaction Type=Refund → refund kind, regardless of Net sign."""
    statement = extract_toast_csv(_load_fixture())
    refund_rows = [r for r in statement.transactions if r.kind == "refund"]
    assert len(refund_rows) == 3


def test_void_rows_map_to_adjustment_not_gross_charge() -> None:
    """Voids are scratch entries — they must NOT inflate gross_charge.
    Map to adjustment (excluded from identity)."""
    statement = extract_toast_csv(_load_fixture())
    adjustment_rows = [r for r in statement.transactions if r.kind == "adjustment"]
    # Two voided rows in the fixture → 2 adjustment rows.
    assert len(adjustment_rows) == 2
    # Confirm void amounts are NOT included in gross.
    void_total = sum((r.amount for r in adjustment_rows), Decimal("0.00"))
    assert void_total == Decimal("83.50")  # 65.00 + 18.50


# ---------------------------------------------------------------------------
# Money math — assert to the cent
# ---------------------------------------------------------------------------


def test_aggregates_match_fixture_identity_math() -> None:
    """Tie-out values match the fixture math exactly.

    Payments (15):  28.50+45.75+18.00+35.25+78.40+25.00+22.85+52.10+112.30
                  +15.50+41.75+89.00+33.60+145.85+19.95             = 763.80
    Refunds (3):    15.00+22.50+8.75                                 =  46.25
    Chargebacks:    none                                             =   0.00
    Fees:           Toast doesn't report fees                        =   0.00
    Synthetic payout = 763.80 - 46.25 - 0.00 - 0.00                  = 717.55
    """
    statement = extract_toast_csv(_load_fixture())
    agg = aggregate_processor(statement.transactions)

    assert agg.gross_volume.value == Decimal("763.80")
    assert agg.refunds_total.value == Decimal("46.25")
    assert agg.chargebacks_total.value == Decimal("0.00")
    assert agg.fees_total.value == Decimal("0.00")
    assert agg.payouts_total.value == Decimal("717.55")
    # Identity holds within tolerance.
    expected_payouts = (
        agg.gross_volume.value
        - agg.refunds_total.value
        - agg.chargebacks_total.value
        - agg.fees_total.value
    )
    assert abs(expected_payouts - agg.payouts_total.value) < Decimal("0.01")


def test_validator_passes_on_sample_fixture() -> None:
    """The deterministic gate must accept the fixture — the printed totals
    are summed from rows, so the per-kind tie-out is trivially true."""
    statement = extract_toast_csv(_load_fixture())
    result = validate_processor(statement)
    assert result.passed is True, f"validation failures: {result.failures}"


def test_avg_daily_volume_uses_period_days_denominator() -> None:
    """avg_daily_volume = gross / period_days (inclusive of both endpoints).
    Locks the denominator into the contract."""
    statement = extract_toast_csv(_load_fixture())
    base = aggregate_processor(statement.transactions)
    dossier = build_stripe_dossier_aggregates(statement, base)
    # May 1 → May 30 inclusive = 30 days.
    assert dossier.period_days == 30
    expected = (Decimal("763.80") / Decimal(30)).quantize(Decimal("0.01"))
    assert dossier.avg_daily_volume == expected


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_extract_empty_buffer_raises() -> None:
    with pytest.raises(ToastCsvError, match="empty CSV buffer"):
        extract_toast_csv(b"")


def test_extract_missing_required_columns_raises() -> None:
    """A CSV missing the Toast-unique columns (Revenue Center / Dining
    Options) can't be discriminated as Toast — fail closed."""
    bad_csv = (
        b"Date,Server,Order ID,Net Amount,Transaction Type\n"
        b"2026-05-01,Alex,toast_ord_1,12.50,Payment\n"
    )
    with pytest.raises(ToastCsvError, match="missing required columns"):
        extract_toast_csv(bad_csv)


def test_unknown_transaction_type_defaults_to_adjustment() -> None:
    """CLAUDE.md scoring discipline: unknown transaction types must NEVER
    default to gross_charge — they'd inflate revenue silently. The Toast
    extractor maps unknown values to adjustment instead."""
    header = (
        b"Date,Server,Order ID,Order #,Location,Revenue Center,Tab Name,"
        b"Item,Qty,Gross Amount,Discount Amount,Net Amount,Void,Void Reason,"
        b"Check Amount,Tip Amount,Total Amount,Transaction Type,Payment Type,"
        b"Last 4,Card Brand,Card Holder,Dining Options\n"
    )
    data = (
        b"2026-05-15 10:00:00,Alex,toast_mystery,O-9999,Main Street,Bar,"
        b"Mystery Tab,Mystery Item,1,99.99,0.00,99.99,No,,99.99,0.00,99.99,"
        b"SomeNewToastType,Card,4242,Visa,Guest,Dine In\n"
    )
    csv_bytes = header + data
    statement = extract_toast_csv(csv_bytes)
    # The one row should land as adjustment, NOT as gross_charge.
    # (Synthetic payout row gets added at period end — exclude it from
    # the assertion since it's kind="payout", not "gross_charge".)
    non_payout = [r for r in statement.transactions if r.kind != "payout"]
    assert all(r.kind != "gross_charge" for r in non_payout)
    # And the gross_volume in the summary should be zero.
    assert statement.summary.gross_volume == Decimal("0.00")
