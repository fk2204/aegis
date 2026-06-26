"""Tests for the ``McaPosition.match_source`` split (2026-06-26).

Verifies:

* GENERIC_MCA_TERMS tightening — single-word terms (``advance`` /
  ``factor`` / ``holdback`` / ``receivables`` / ``merchant svc`` /
  ``remit``) no longer match. A merchant with 96 routine "advance"
  rows used to surface 96 false-positive positions; the same input
  now yields zero.
* ``_detect_mca_positions`` sets ``match_source = "known_funder"``
  when the description hit ``KNOWN_FUNDERS`` and ``"pattern"`` when
  it only matched a tightened ``GENERIC_MCA_TERMS`` phrase under the
  daily-cadence guard.
* DTO ser/de carries the new field through ``pattern_analysis_to_dto``
  + ``pattern_analysis_from_dto`` so persisted rows survive the round
  trip.
* ``count_confirmed_positions`` + ``count_pattern_positions`` total to
  ``len(positions)`` — exhaustive partition.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import (
    GENERIC_MCA_TERMS,
    _detect_mca_positions,
    analyze_patterns,
    count_confirmed_positions,
    count_pattern_positions,
    pattern_analysis_from_dto,
    pattern_analysis_to_dto,
)


def _debit(
    *,
    posted_date: date,
    description: str,
    amount: Decimal = Decimal("-150.00"),
) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        running_balance=None,
        source_page=1,
        source_line=1,
        category="mca_debit",
        classification_confidence=95,
    )


def _weekday_stream(
    description: str,
    *,
    start: date = date(2026, 4, 1),
    days: int = 20,
    amount: Decimal = Decimal("-150.00"),
) -> list[ClassifiedTransaction]:
    """Build a weekday-only daily debit stream of ``days`` business days."""
    rows: list[ClassifiedTransaction] = []
    cur = start
    while len(rows) < days:
        if cur.weekday() < 5:
            rows.append(_debit(posted_date=cur, description=description, amount=amount))
        cur += timedelta(days=1)
    return rows


# ----------------------------------------------------------------------
# GENERIC_MCA_TERMS tightening
# ----------------------------------------------------------------------


def test_tightened_terms_drop_single_word_generics() -> None:
    """Single-word generics were the root cause of 16-96 false positives.
    The live list must contain only multi-word / MCA-specific phrases."""
    for banned in ("advance", "remit", "factor", "holdback", "receivables", "merchant svc"):
        assert banned not in GENERIC_MCA_TERMS, (
            f"'{banned}' is too generic — it appears in countless legitimate "
            "transactions and was the source of 16-96 false MCA positions."
        )


def test_tightened_terms_keep_load_bearing_phrases() -> None:
    """The phrases the operator's spec deliberately retains."""
    for keeper in (
        "daily pmt",
        "daily payment",
        "daily business pmt",
        "daily remittance",
        "ach daily",
        "daily debit",
        "business daily",
        "daily withdrawal",
        "future receipts",
        "daily ach",
        "receivable purchase",
        "biz advance",
        "rcvbl",
    ):
        assert keeper in GENERIC_MCA_TERMS, f"'{keeper}' was on the operator's keep-list."


def test_routine_advance_rows_no_longer_overcount() -> None:
    """Regression: 50 routine ``ACH DEBIT INVESTMENT ADVANCE`` rows used
    to false-positive as an MCA position via the ``advance`` term + the
    cadence guard. After the tightening, zero positions are returned."""
    rows = _weekday_stream("ACH DEBIT INVESTMENT ADVANCE", days=50)
    positions = _detect_mca_positions(
        rows, period_start=date(2026, 4, 1), period_end=date(2026, 6, 30)
    )
    assert positions == []


# ----------------------------------------------------------------------
# match_source split — confirmed vs pattern
# ----------------------------------------------------------------------


def test_known_funder_match_yields_confirmed_bucket() -> None:
    """A ``KNOWN_FUNDERS`` substring (here ``ondeck``) marks the position
    as confirmed regardless of how many times it fires."""
    rows = _weekday_stream("ACH DEBIT ONDECK DAILY PMT", days=12)
    positions = _detect_mca_positions(
        rows, period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )
    assert len(positions) == 1
    assert positions[0].match_source == "known_funder"
    assert count_confirmed_positions(positions) == 1
    assert count_pattern_positions(positions) == 0


def test_pattern_only_match_yields_pattern_bucket() -> None:
    """A retained ``GENERIC_MCA_TERMS`` phrase (here ``daily remittance``)
    without any KNOWN_FUNDERS substring fires under the daily-cadence
    guard and lands in the ``pattern`` bucket."""
    rows = _weekday_stream("ACH DEBIT DAILY REMITTANCE", days=12)
    positions = _detect_mca_positions(
        rows, period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )
    assert len(positions) == 1
    assert positions[0].match_source == "pattern"
    assert count_confirmed_positions(positions) == 0
    assert count_pattern_positions(positions) == 1


def test_mixed_buckets_partition_exhaustively() -> None:
    """Two streams — one named-funder (confirmed), one pattern-only
    (pattern). The two counts must sum to the total list length."""
    rows = _weekday_stream("ACH DEBIT KAPITUS DAILY REMIT", days=12)
    rows += _weekday_stream(
        "ACH DEBIT FUTURE RECEIPTS",
        start=date(2026, 4, 1),
        days=12,
    )
    positions = _detect_mca_positions(
        rows, period_start=date(2026, 4, 1), period_end=date(2026, 4, 30)
    )
    assert len(positions) == 2
    assert (count_confirmed_positions(positions) + count_pattern_positions(positions)) == len(
        positions
    )


# ----------------------------------------------------------------------
# DTO round-trip
# ----------------------------------------------------------------------


def test_match_source_round_trips_through_dto() -> None:
    """Persistence must preserve the bucket — a confirmed position
    stays confirmed after pattern_analysis -> DTO -> pattern_analysis."""
    rows = _weekday_stream("ACH DEBIT ONDECK DAILY PMT", days=12)
    rows += _weekday_stream(
        "ACH DEBIT DAILY DEBIT",
        start=date(2026, 4, 1),
        days=12,
    )
    pa = analyze_patterns(
        [*rows],  # debits-only input is fine; analyzer flattens
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    dto = pattern_analysis_to_dto(pa)
    restored = pattern_analysis_from_dto(dto)

    sources_before = sorted(p.match_source for p in pa.mca_positions)
    sources_after = sorted(p.match_source for p in restored.mca_positions)
    assert sources_before == sources_after
    # And at least one of each — the test setup guarantees both buckets.
    assert "known_funder" in sources_after
    assert "pattern" in sources_after
