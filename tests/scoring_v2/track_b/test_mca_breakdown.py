"""Tests for the Track B MCA-position breakdown (2026-06-26).

``compute_mca_position_breakdown`` splits the LLM's ``mca_debit``
classifications into ``confirmed`` (description carries a KNOWN_FUNDERS
substring — high-confidence named-funder match) and ``pattern`` (no
named funder — verify before treating as stacking). The split feeds
``frame_mca_positions`` so the dossier renders "N confirmed; M possible
via payment pattern (verify)" instead of one combined number.

Surface coverage:
* breakdown function partitions ``mca_debit`` rows exhaustively.
* ``frame_mca_positions`` text shows confirmed + pattern separately when
  both are provided; falls back to legacy wording when called without
  the kwargs (back-compat).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from aegis.parser.models import ClassifiedTransaction
from aegis.scoring_v2.track_b.framing import frame_mca_positions
from aegis.scoring_v2.track_b.signals import compute_mca_position_breakdown


def _mca(description: str) -> ClassifiedTransaction:
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=date(2026, 4, 1),
        description=description,
        amount=Decimal("-150.00"),
        running_balance=None,
        source_page=1,
        source_line=1,
        category="mca_debit",
        classification_confidence=95,
    )


# ----------------------------------------------------------------------
# compute_mca_position_breakdown
# ----------------------------------------------------------------------


def test_breakdown_all_confirmed_when_named_funders_match() -> None:
    txns = {
        "doc1": [
            _mca("ACH DEBIT ONDECK DAILY PMT"),
            _mca("ACH DEBIT KAPITUS DAILY REMIT"),
            _mca("ACH DEBIT KABBAGE BUSINESS"),
        ]
    }
    confirmed, pattern = compute_mca_position_breakdown(txns)
    assert confirmed == 3
    assert pattern == 0


def test_breakdown_all_pattern_when_no_named_funder() -> None:
    txns = {
        "doc1": [
            _mca("ACH DEBIT DAILY REMITTANCE"),
            _mca("ACH DEBIT FUTURE RECEIPTS"),
            _mca("ACH DEBIT GENERIC PROCESSOR"),
        ]
    }
    confirmed, pattern = compute_mca_position_breakdown(txns)
    assert confirmed == 0
    assert pattern == 3


def test_breakdown_partitions_mixed_descriptions() -> None:
    txns = {
        "doc1": [
            _mca("ACH DEBIT ONDECK DAILY PMT"),
            _mca("ACH DEBIT DAILY REMITTANCE"),
            _mca("ACH DEBIT KAPITUS DAILY REMIT"),
            _mca("ACH DEBIT FUTURE RECEIPTS"),
        ]
    }
    confirmed, pattern = compute_mca_position_breakdown(txns)
    assert confirmed == 2
    assert pattern == 2


def test_breakdown_ignores_non_mca_categories() -> None:
    """Only ``mca_debit`` rows count — a deposit / nsf_fee / fee row
    with a funder-like description is irrelevant to the MCA bucket."""
    txns = {
        "doc1": [
            _mca("ACH DEBIT ONDECK DAILY PMT"),
            ClassifiedTransaction(
                id=uuid4(),
                posted_date=date(2026, 4, 1),
                description="ACH CREDIT ONDECK REFUND",  # not mca_debit
                amount=Decimal("100.00"),
                running_balance=None,
                source_page=1,
                source_line=2,
                category="deposit",
                classification_confidence=95,
            ),
        ]
    }
    confirmed, pattern = compute_mca_position_breakdown(txns)
    assert confirmed == 1
    assert pattern == 0


# ----------------------------------------------------------------------
# frame_mca_positions — split-aware text
# ----------------------------------------------------------------------


def test_frame_shows_split_when_both_buckets_have_counts() -> None:
    reason = frame_mca_positions(4, "elevated", confirmed_count=2, pattern_count=2)
    assert "2 confirmed MCA positions" in reason.detail
    assert "funder name detected" in reason.detail
    assert "2 possible via payment pattern" in reason.detail
    assert "verify" in reason.detail.lower()


def test_frame_shows_only_confirmed_when_no_pattern() -> None:
    reason = frame_mca_positions(3, "elevated", confirmed_count=3, pattern_count=0)
    assert "3 confirmed MCA positions" in reason.detail
    assert "possible via payment pattern" not in reason.detail


def test_frame_shows_only_pattern_when_no_confirmed() -> None:
    reason = frame_mca_positions(2, "elevated", confirmed_count=0, pattern_count=2)
    assert "2 possible via payment pattern" in reason.detail
    assert "confirmed MCA positions" not in reason.detail


def test_frame_zero_count_unchanged() -> None:
    """No MCA debits → existing wording, not the split text."""
    reason = frame_mca_positions(0, "positive")
    assert "No MCA debit transactions detected" in reason.detail


def test_frame_legacy_call_without_kwargs_works() -> None:
    """Back-compat: callers that don't carry the breakdown still
    produce the historical multi-position wording."""
    reason = frame_mca_positions(3, "elevated")
    assert "3 MCA debit transactions observed" in reason.detail
    # Did NOT crash on the new kwargs being None.
