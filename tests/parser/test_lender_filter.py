"""Deterministic lender-proceeds filter (R0.2 from the 2026-06-08 audit).

Two scopes:
  1. Direct ``is_lender_proceed`` API — substring match, case-insensitive,
     punctuation-stripped normalization, conservative on bare brand names
     ("SQUARE" alone must NOT match — it's processor settlement).
  2. End-to-end via ``aggregate()`` — a misclassified MCA deposit must
     drop out of ``true_revenue`` and surface a ``lender_proceeds_excluded``
     flag with the matched name so the dossier can show WHY.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.parser.aggregate import AggregateResult, aggregate
from aegis.parser.lender_filter import KNOWN_LENDERS, is_lender_proceed
from aegis.parser.models import ClassifiedTransaction, TransactionCategory

PERIOD_START = date(2026, 3, 1)
PERIOD_END = date(2026, 3, 31)
BEGINNING = Decimal("100000.00")


def _txn(
    *,
    amount: Decimal,
    category: TransactionCategory,
    description: str = "row",
    posted: date = date(2026, 3, 15),
    running_balance: Decimal | None = None,
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted,
        description=description,
        amount=amount,
        running_balance=running_balance,
        source_page=1,
        source_line=1,
        category=category,
        classification_confidence=95,
    )


# ---------------------------------------------------------------------------
# is_lender_proceed direct tests
# ---------------------------------------------------------------------------


def test_ondeck_funding_substring_matches() -> None:
    matched, name = is_lender_proceed("ONDECK FUNDING ADV")
    assert matched is True
    assert name == "ONDECK"


def test_case_insensitive_match() -> None:
    matched, name = is_lender_proceed("ondeck funding")
    assert matched is True
    assert name == "ONDECK"


def test_multi_word_revenue_based_financing() -> None:
    matched, name = is_lender_proceed("REVENUE BASED FINANCING ACH")
    assert matched is True
    assert name == "REVENUE BASED FINANCING"


def test_revenue_based_lending_variant() -> None:
    matched, name = is_lender_proceed("Revenue Based Lending deposit 123")
    assert matched is True
    assert name == "REVENUE BASED LENDING"


def test_punctuation_stripped() -> None:
    """Comma / hyphen / dot in the descriptor must not break the match."""
    matched, name = is_lender_proceed("ONDECK-FUNDING, INC.")
    assert matched is True
    assert name == "ONDECK"


def test_bare_square_is_not_lender() -> None:
    """Conservative: bare ``SQUARE`` is settlement processor, not lender."""
    matched, name = is_lender_proceed("SQUARE INC DEPOSIT")
    assert matched is False
    assert name is None


def test_bare_stripe_is_not_lender() -> None:
    matched, name = is_lender_proceed("STRIPE TRANSFER PAYOUT")
    assert matched is False
    assert name is None


def test_bare_paypal_is_not_lender() -> None:
    matched, name = is_lender_proceed("PAYPAL TRANSFER")
    assert matched is False
    assert name is None


def test_square_capital_matches() -> None:
    matched, name = is_lender_proceed("SQUARE CAPITAL ADVANCE")
    assert matched is True
    assert name == "SQUARE CAPITAL"


def test_shopify_capital_matches() -> None:
    matched, name = is_lender_proceed("SHOPIFY CAPITAL LOAN DEPOSIT")
    assert matched is True
    assert name == "SHOPIFY CAPITAL"


def test_paypal_working_capital_matches() -> None:
    matched, name = is_lender_proceed("PayPal Working Capital advance")
    assert matched is True
    assert name == "PAYPAL WORKING CAPITAL"


def test_sba_eidl_matches() -> None:
    matched, name = is_lender_proceed("SBA EIDL LOAN PROCEEDS")
    assert matched is True
    assert name == "SBA EIDL"


def test_line_of_credit_matches() -> None:
    matched, name = is_lender_proceed("LINE OF CREDIT DRAW BANK XYZ")
    assert matched is True
    assert name == "LINE OF CREDIT"


def test_empty_description() -> None:
    matched, name = is_lender_proceed("")
    assert matched is False
    assert name is None


def test_unrelated_description() -> None:
    matched, name = is_lender_proceed("ACH CREDIT FROM ACME CORP SALES")
    assert matched is False
    assert name is None


def test_longest_match_wins() -> None:
    """When two keys could match, the longest one is returned."""
    # "PEARL CAPITAL" must beat any shorter accidental overlap.
    matched, name = is_lender_proceed("PEARL CAPITAL FUNDING ADV")
    assert matched is True
    assert name == "PEARL CAPITAL"


def test_known_lenders_set_is_frozen() -> None:
    assert isinstance(KNOWN_LENDERS, frozenset)
    # Sanity-check a representative slice — proves the set was loaded
    # and the canonical entries didn't drift.
    assert "ONDECK" in KNOWN_LENDERS
    assert "SQUARE CAPITAL" in KNOWN_LENDERS
    assert "SBA EIDL" in KNOWN_LENDERS
    assert "REVENUE BASED FINANCING" in KNOWN_LENDERS
    # Bare brands MUST NOT be in the set — they alias processors.
    assert "SQUARE" not in KNOWN_LENDERS
    assert "STRIPE" not in KNOWN_LENDERS
    assert "PAYPAL" not in KNOWN_LENDERS
    assert "SHOPIFY" not in KNOWN_LENDERS


# ---------------------------------------------------------------------------
# End-to-end via aggregate()
# ---------------------------------------------------------------------------


def _aggregate(txns: list[ClassifiedTransaction]) -> AggregateResult:
    return aggregate(
        txns,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        beginning_balance=BEGINNING,
    )


def test_ondeck_ach_credit_excluded_from_revenue() -> None:
    """A $25K OnDeck deposit misclassified as ``ach_credit`` must drop
    out of true_revenue and surface a ``lender_proceeds_excluded`` flag.
    """
    txns = [
        _txn(amount=Decimal("10000.00"), category="deposit"),
        _txn(
            amount=Decimal("25000.00"),
            category="ach_credit",
            description="ONDECK FUNDING ADV 12345",
        ),
    ]
    result = _aggregate(txns)
    assert result.aggregates.true_revenue.value == Decimal("10000.00")
    # Exclusion flag must mention the matched name + amount.
    excl_flags = [
        f for f in result.flags if f.startswith("lender_proceeds_excluded:")
    ]
    assert len(excl_flags) == 1
    assert "ONDECK" in excl_flags[0]
    assert "25000.00" in excl_flags[0]


def test_legitimate_square_deposit_counts_as_revenue() -> None:
    """SQUARE INC settlement deposit is real revenue — must NOT be excluded."""
    txns = [
        _txn(
            amount=Decimal("8500.00"),
            category="deposit",
            description="DEPOSIT SQUARE INC PAYOUT",
        ),
    ]
    result = _aggregate(txns)
    assert result.aggregates.true_revenue.value == Decimal("8500.00")
    assert not any(
        f.startswith("lender_proceeds_excluded") for f in result.flags
    )


def test_square_capital_advance_excluded() -> None:
    """SQUARE CAPITAL advance IS lender proceeds — must be excluded."""
    txns = [
        _txn(
            amount=Decimal("12000.00"),
            category="deposit",
            description="SQUARE CAPITAL ADVANCE FUNDING",
        ),
    ]
    result = _aggregate(txns)
    assert result.aggregates.true_revenue.value == Decimal("0.00")
    assert any(
        "SQUARE CAPITAL" in f and "12000.00" in f
        for f in result.flags
        if f.startswith("lender_proceeds_excluded:")
    )


def test_revenue_based_financing_excluded() -> None:
    txns = [
        _txn(amount=Decimal("5000.00"), category="deposit"),
        _txn(
            amount=Decimal("30000.00"),
            category="ach_credit",
            description="REVENUE BASED FINANCING ACH DEPOSIT",
        ),
    ]
    result = _aggregate(txns)
    assert result.aggregates.true_revenue.value == Decimal("5000.00")


def test_excluded_rows_not_in_true_revenue_source_ids() -> None:
    """Audit-trail: the excluded row's UUID must NOT appear in the
    true_revenue ``source_ids`` (only contributors land there). The
    UUID is still discoverable from the per-row exclusion flag below.
    """
    real_dep = _txn(amount=Decimal("10000.00"), category="deposit")
    lender = _txn(
        amount=Decimal("25000.00"),
        category="ach_credit",
        description="ONDECK FUNDING ADV",
    )
    result = _aggregate([real_dep, lender])
    src_ids = result.aggregates.true_revenue.source_ids
    assert real_dep.id in src_ids
    assert lender.id not in src_ids
    # The per-row flag carries the excluded row's UUID for drill-down.
    row_flags = [
        f for f in result.flags if f.startswith("lender_proceeds_excluded_row:")
    ]
    assert any(str(lender.id) in f for f in row_flags)


def test_no_double_subtraction_with_transfer() -> None:
    """Lender filter is additive to transfer logic — a +transfer row stays
    on the transfer-subtraction path (never reaches the lender filter)
    so revenue math doesn't double-count its impact.
    """
    txns = [
        _txn(amount=Decimal("10000.00"), category="deposit"),
        # Owner transfer IN, no lender keyword — only the transfer
        # subtraction path applies.
        _txn(
            amount=Decimal("2000.00"),
            category="transfer",
            description="ONLINE BANKING TRANSFER FROM CHK 1234",
        ),
    ]
    result = _aggregate(txns)
    assert result.aggregates.true_revenue.value == Decimal("8000.00")
    assert not any(
        f.startswith("lender_proceeds_excluded") for f in result.flags
    )


def test_multiple_lender_deposits_all_excluded() -> None:
    txns = [
        _txn(amount=Decimal("5000.00"), category="deposit"),
        _txn(
            amount=Decimal("15000.00"),
            category="ach_credit",
            description="ONDECK FUNDING",
        ),
        _txn(
            amount=Decimal("20000.00"),
            category="ach_credit",
            description="KAPITUS ACH CREDIT",
        ),
        _txn(
            amount=Decimal("8000.00"),
            category="deposit",
            description="SBA EIDL LOAN PROCEEDS",
        ),
    ]
    result = _aggregate(txns)
    # Only the $5K real deposit counts.
    assert result.aggregates.true_revenue.value == Decimal("5000.00")
    excl_flag = next(
        f for f in result.flags if f.startswith("lender_proceeds_excluded:")
    )
    # All three matched names should appear in the summary flag.
    assert "ONDECK" in excl_flag
    assert "KAPITUS" in excl_flag
    assert "SBA EIDL" in excl_flag
    assert "43000.00" in excl_flag
