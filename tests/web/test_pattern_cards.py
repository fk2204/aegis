"""Tests for the pattern-card builder used on merchant_detail.

Covers:
- Every code in ``PATTERN_COPY`` produces a valid card.
- Cards are sorted by severity descending.
- ``mca_stacking`` is skipped (rendered as the dedicated StackingCard).
- ``recent_account_opening`` and ``payroll_absent`` appear as cards
  with empty source_transactions — their drill-down renders an
  explanation panel (the evidence partial dispatches to one).
- Unknown pattern codes are silently skipped (don't crash the page if a
  new detector lands without operator copy yet).
- ``None`` pattern_analysis returns an empty list.
- ``source_ids`` that don't match a transaction are silently dropped.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import Pattern, PatternAnalysis
from aegis.web._pattern_cards import (
    PATTERN_COPY,
    build_pattern_cards,
)


def _txn(amount: str = "100.00") -> ClassifiedTransaction:
    return ClassifiedTransaction(
        posted_date=date(2026, 1, 15),
        description="ACME CORP DEPOSIT",
        amount=Decimal(amount),
        source_page=1,
        source_line=1,
        category="deposit",
        classification_confidence=90,
    )


def _empty_analysis(patterns: list[Pattern]) -> PatternAnalysis:
    return PatternAnalysis(
        patterns=patterns,
        mca_positions=[],
        has_kiting=False,
        paydown_suspected=False,
    )


@pytest.mark.parametrize("code", sorted(PATTERN_COPY.keys()))
def test_every_pattern_copy_code_produces_a_card(code: str) -> None:
    """Every code in PATTERN_COPY must round-trip into a renderable card."""
    txn = _txn()
    pattern = Pattern(
        code=code,
        severity=25,
        detail="3 events over 14 days",
        source_ids=[txn.id],
    )
    cards = build_pattern_cards(_empty_analysis([pattern]), [txn])

    assert len(cards) == 1
    card = cards[0]
    assert card.code == code
    assert card.title == PATTERN_COPY[code].title
    assert card.description == PATTERN_COPY[code].description
    assert card.detail == "3 events over 14 days"
    assert card.severity == 25
    assert card.severity_band in {"pos", "warn", "neg"}
    assert card.source_transactions == [txn]


def test_cards_sorted_by_severity_descending() -> None:
    txn = _txn()
    patterns = [
        Pattern(code="round_number_deposits", severity=15, detail="a", source_ids=[txn.id]),
        Pattern(code="wash_deposit_suspected", severity=35, detail="b", source_ids=[txn.id]),
        Pattern(code="duplicate_deposits_detected", severity=30, detail="c", source_ids=[txn.id]),
    ]
    cards = build_pattern_cards(_empty_analysis(patterns), [txn])

    assert [c.severity for c in cards] == [35, 30, 15]
    assert [c.code for c in cards] == [
        "wash_deposit_suspected",
        "duplicate_deposits_detected",
        "round_number_deposits",
    ]


def test_mca_stacking_is_skipped() -> None:
    """mca_stacking has its own richer card; must not appear in the list."""
    txn = _txn()
    patterns = [
        Pattern(code="mca_stacking", severity=50, detail="3 positions", source_ids=[txn.id]),
        Pattern(code="round_number_deposits", severity=15, detail="x", source_ids=[txn.id]),
    ]
    cards = build_pattern_cards(_empty_analysis(patterns), [txn])

    assert [c.code for c in cards] == ["round_number_deposits"]


def test_recent_account_opening_appears_with_empty_source_transactions() -> None:
    """recent_account_opening now renders as a pattern card so the
    explanation panel sits next to the flag. source_transactions is
    empty because the detector emits no source_ids (the flag fires off
    the period start vs today, not a specific transaction)."""
    txn = _txn()
    patterns = [
        Pattern(code="recent_account_opening", severity=15, detail="opened 30d ago", source_ids=[]),
    ]
    cards = build_pattern_cards(_empty_analysis(patterns), [txn])

    assert [c.code for c in cards] == ["recent_account_opening"]
    assert cards[0].source_transactions == []


def test_payroll_absent_appears_with_empty_source_transactions() -> None:
    """payroll_absent is a presence-of-absence flag — the explanation
    panel summarizes the period and revenue context that triggered it."""
    txn = _txn()
    patterns = [
        Pattern(
            code="payroll_absent",
            severity=10,
            detail="no payroll across period",
            source_ids=[],
        ),
    ]
    cards = build_pattern_cards(_empty_analysis(patterns), [txn])

    assert [c.code for c in cards] == ["payroll_absent"]
    assert cards[0].source_transactions == []


def test_unknown_pattern_codes_are_silently_skipped() -> None:
    """A new detector without operator copy must not crash the dashboard."""
    txn = _txn()
    patterns = [
        Pattern(code="brand_new_detector_v2", severity=42, detail="unrelated", source_ids=[]),
        Pattern(code="round_number_deposits", severity=15, detail="x", source_ids=[txn.id]),
    ]
    cards = build_pattern_cards(_empty_analysis(patterns), [txn])

    assert [c.code for c in cards] == ["round_number_deposits"]


def test_none_pattern_analysis_returns_empty_list() -> None:
    assert build_pattern_cards(None, []) == []


def test_unknown_source_ids_are_dropped() -> None:
    """source_ids referring to transactions absent from the list don't crash."""
    txn = _txn()
    bogus = uuid4()
    pattern = Pattern(
        code="round_number_deposits",
        severity=15,
        detail="x",
        source_ids=[txn.id, bogus],
    )
    cards = build_pattern_cards(_empty_analysis([pattern]), [txn])

    assert len(cards) == 1
    assert cards[0].source_transactions == [txn]


def test_severity_band_thresholds() -> None:
    """Verify the band buckets: >=30 neg, >=15 warn, else pos."""
    txn = _txn()
    patterns = [
        Pattern(code="round_number_deposits", severity=14, detail="a", source_ids=[txn.id]),
        Pattern(code="duplicate_deposits_detected", severity=15, detail="b", source_ids=[txn.id]),
        Pattern(code="wash_deposit_suspected", severity=30, detail="c", source_ids=[txn.id]),
    ]
    cards = build_pattern_cards(_empty_analysis(patterns), [txn])
    by_code = {c.code: c.severity_band for c in cards}

    assert by_code["round_number_deposits"] == "pos"
    assert by_code["duplicate_deposits_detected"] == "warn"
    assert by_code["wash_deposit_suspected"] == "neg"
