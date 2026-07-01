"""Tests that MCA position counting counts distinct funder streams,
not raw ``mca_debit`` rows.

Regression: 2026-07-01 — Turnbull's dossier showed "11 confirmed, 22
possible" for a merchant whose stated stack was Fundworks + Headway
(2 lenders). Root cause: ``compute_mca_position_count`` was counting
every ``mca_debit`` transaction row, so 30 daily Headway debits + 3
Fundworks debits surfaced as 33 positions instead of 2. Fix: delegate
to the parser's ``_detect_mca_positions`` grouping.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.track_b.signals import (
    compute_mca_position_breakdown,
    compute_mca_position_count,
)


def _make_debit(
    description: str,
    posted: date,
    amount: str = "-500.00",
    line: int = 1,
) -> ClassifiedTransaction:
    """Build a minimal ClassifiedTransaction shaped like a real debit."""
    return ClassifiedTransaction(
        posted_date=posted,
        description=description,
        amount=Decimal(amount),
        running_balance=None,
        source_page=1,
        source_line=line,
        category="mca_debit",
        classification_confidence=90,
    )


def test_single_funder_30_daily_debits_is_1_position() -> None:
    """30 daily Headway debits should surface as 1 distinct position."""
    txns = [
        _make_debit("HEADWAY CAPITAL DAILY PMT", date(2026, 1, i + 1), line=i + 1)
        for i in range(30)
    ]
    count = compute_mca_position_count({"doc1": txns}, date(2026, 1, 1), date(2026, 1, 31))
    assert count == 1, f"Expected 1 position, got {count}"


def test_two_funders_is_2_positions() -> None:
    """Headway + Fundworks = 2 positions regardless of daily-row count."""
    headway = [
        _make_debit("HEADWAY CAPITAL DAILY PMT", date(2026, 1, i + 1), line=i + 1)
        for i in range(20)
    ]
    fundworks = [
        _make_debit("FUNDWORKS LLC ACH DEBIT", date(2026, 1, i + 1), line=i + 100)
        for i in range(20)
    ]
    count = compute_mca_position_count(
        {"doc1": headway + fundworks}, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert count == 2, f"Expected 2 positions, got {count}"


def test_three_funders_across_docs_is_3_positions() -> None:
    """Three named funders spread across two documents = 3 distinct positions."""
    headway = [
        _make_debit("HEADWAY CAPITAL DAILY", date(2026, 1, i + 1), line=i + 1) for i in range(15)
    ]
    fundworks = [
        _make_debit("FUNDWORKS LLC DAILY REMITTANCE", date(2026, 1, i + 1), line=i + 30)
        for i in range(15)
    ]
    revenued = [
        _make_debit("REVENUED LLC PAYMENT", date(2026, 1, i + 1), line=i + 60) for i in range(15)
    ]
    count = compute_mca_position_count(
        {"doc1": headway + fundworks, "doc2": revenued},
        date(2026, 1, 1),
        date(2026, 1, 31),
    )
    assert count == 3, f"Expected 3 positions, got {count}"


def test_breakdown_confirmed_vs_pattern() -> None:
    """Named-funder rows land in ``confirmed``; generic daily-cadence
    matches land in ``pattern``. Sum = compute_mca_position_count."""
    known = [
        _make_debit("HEADWAY CAPITAL PMT", date(2026, 1, i + 1), line=i + 1) for i in range(15)
    ]
    generic = [
        _make_debit("DAILY REMITTANCE PAYMENT", date(2026, 1, i + 1), line=i + 100)
        for i in range(15)
    ]
    confirmed, pattern = compute_mca_position_breakdown(
        {"doc1": known + generic}, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert confirmed == 1, f"Expected 1 confirmed, got {confirmed}"
    assert pattern == 1, f"Expected 1 pattern, got {pattern}"

    total = compute_mca_position_count(
        {"doc1": known + generic}, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert total == confirmed + pattern


def test_below_frequency_gate_is_zero_positions() -> None:
    """Fewer than 3 occurrences of a funder = not a position (gate
    inherited from ``_detect_mca_positions``)."""
    two_debits = [
        _make_debit("HEADWAY CAPITAL DAILY PMT", date(2026, 1, i + 1), line=i + 1) for i in range(2)
    ]
    count = compute_mca_position_count({"doc1": two_debits}, date(2026, 1, 1), date(2026, 1, 31))
    assert count == 0


def test_empty_bundle_returns_zero() -> None:
    count = compute_mca_position_count({}, date(2026, 1, 1), date(2026, 1, 31))
    assert count == 0
    confirmed, pattern = compute_mca_position_breakdown({}, date(2026, 1, 1), date(2026, 1, 31))
    assert (confirmed, pattern) == (0, 0)


def test_period_defaults_to_bundle_span_when_not_provided() -> None:
    """Callers that don't pass period_start/end still get correct
    counting — the widest posted_date span is used."""
    txns = [
        _make_debit("HEADWAY CAPITAL DAILY", date(2026, 2, i + 1), line=i + 1) for i in range(20)
    ]
    count = compute_mca_position_count({"doc1": txns})
    assert count == 1


def test_fundworks_is_recognised_as_known_funder() -> None:
    """2026-07-01 KNOWN_FUNDERS addition — verified via the confirmed
    bucket."""
    txns = [
        _make_debit("FUNDWORKS LLC ACH DAILY", date(2026, 1, i + 1), line=i + 1) for i in range(20)
    ]
    confirmed, pattern = compute_mca_position_breakdown(
        {"doc1": txns}, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert confirmed == 1
    assert pattern == 0


def test_revenued_is_recognised_as_known_funder() -> None:
    """2026-07-01 KNOWN_FUNDERS addition — verified via the confirmed
    bucket. Transplex uses REVENUED as their MCA lender."""
    txns = [
        _make_debit("REVENUED LLC PAYMENT", date(2026, 1, i + 1), line=i + 1) for i in range(20)
    ]
    confirmed, pattern = compute_mca_position_breakdown(
        {"doc1": txns}, date(2026, 1, 1), date(2026, 1, 31)
    )
    assert confirmed == 1
    assert pattern == 0
