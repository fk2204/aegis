"""InMemoryBankLayoutRepository tests.

Covers the contract that both the in-memory and Supabase backends must
honour:
  * ``upsert_success`` inserts a row on first call (parses=1, fingerprint
    stored), increments + merges on subsequent calls.
  * ``find_by_bank_name`` is case-insensitive.
  * ``get_hints`` returns ``None`` below the 3-parse threshold; returns
    operator hints when at-or-above + non-empty; returns ``None`` when
    hints are whitespace-only even after 3 successful parses.
  * ``set_hints`` creates a primed row (parses=0) when the bank is
    unknown; updates only the hints column on a known row; empty
    string clears.
  * ``list_all`` orders newest-seen first with nulls at the bottom.
"""

from __future__ import annotations

from aegis.bank_layouts.repository import (
    HINTS_AVAILABLE_THRESHOLD,
    InMemoryBankLayoutRepository,
)


def test_upsert_success_first_call_inserts_with_one_parse() -> None:
    repo = InMemoryBankLayoutRepository()
    row = repo.upsert_success(
        bank_name="Chase",
        fingerprint={"transaction_count": 42, "has_running_balance": True},
    )
    assert row.bank_name == "Chase"
    assert row.successful_parses == 1
    assert row.layout_fingerprint == {
        "transaction_count": 42,
        "has_running_balance": True,
    }
    assert row.last_seen is not None
    assert row.extraction_hints is None


def test_upsert_success_second_call_increments_and_merges_fingerprint() -> None:
    repo = InMemoryBankLayoutRepository()
    repo.upsert_success(
        bank_name="Chase",
        fingerprint={"transaction_count": 42, "has_running_balance": True},
    )
    second = repo.upsert_success(
        bank_name="Chase",
        # Overwrites transaction_count; adds page_count + currency.
        fingerprint={"transaction_count": 38, "page_count": 4, "currency": "USD"},
    )
    assert second.successful_parses == 2
    assert second.layout_fingerprint == {
        "transaction_count": 38,
        "has_running_balance": True,
        "page_count": 4,
        "currency": "USD",
    }


def test_find_by_bank_name_is_case_insensitive() -> None:
    repo = InMemoryBankLayoutRepository()
    repo.upsert_success(bank_name="Chase", fingerprint={})

    by_upper = repo.find_by_bank_name("CHASE")
    by_lower = repo.find_by_bank_name("chase")
    by_mixed = repo.find_by_bank_name("Chase")
    assert by_upper is not None
    assert by_lower is not None
    assert by_mixed is not None
    assert by_upper.id == by_lower.id == by_mixed.id


def test_get_hints_returns_none_below_threshold() -> None:
    repo = InMemoryBankLayoutRepository()
    # Prime row with two parses + hints set — still below threshold.
    repo.set_hints(bank_name="Chase", hints="Header is double-line on page 1.")
    repo.upsert_success(bank_name="Chase", fingerprint={})
    repo.upsert_success(bank_name="Chase", fingerprint={})
    row = repo.find_by_bank_name("Chase")
    assert row is not None
    assert row.successful_parses == 2
    assert row.successful_parses < HINTS_AVAILABLE_THRESHOLD
    assert row.extraction_hints == "Header is double-line on page 1."
    # Hints are set but threshold not crossed yet.
    assert repo.get_hints("Chase") is None


def test_get_hints_returns_hints_at_or_above_threshold() -> None:
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Chase", hints="Header is double-line on page 1.")
    for _ in range(HINTS_AVAILABLE_THRESHOLD):
        repo.upsert_success(bank_name="Chase", fingerprint={})
    assert repo.get_hints("Chase") == "Header is double-line on page 1."
    # Case-insensitive lookup honoured here too.
    assert repo.get_hints("CHASE") == "Header is double-line on page 1."


def test_get_hints_returns_none_when_hints_empty_or_whitespace() -> None:
    repo = InMemoryBankLayoutRepository()
    for _ in range(HINTS_AVAILABLE_THRESHOLD + 2):
        repo.upsert_success(bank_name="Chase", fingerprint={})
    # No hints set at all.
    assert repo.get_hints("Chase") is None
    # Operator sets whitespace-only hints — should still gate to None.
    repo.set_hints(bank_name="Chase", hints="   \n  \t  ")
    assert repo.get_hints("Chase") is None


def test_set_hints_creates_primed_row_when_bank_unknown() -> None:
    repo = InMemoryBankLayoutRepository()
    row = repo.set_hints(
        bank_name="Bank of America",
        hints="Multi-column transaction layout.",
    )
    assert row.successful_parses == 0
    assert row.extraction_hints == "Multi-column transaction layout."
    assert row.last_seen is None


def test_set_hints_updates_existing_row_without_touching_parse_count() -> None:
    repo = InMemoryBankLayoutRepository()
    for _ in range(5):
        repo.upsert_success(bank_name="Chase", fingerprint={"k": 1})
    row = repo.find_by_bank_name("Chase")
    assert row is not None
    before_parses = row.successful_parses
    before_last_seen = row.last_seen

    updated = repo.set_hints(bank_name="Chase", hints="Updated hints text.")
    assert updated.extraction_hints == "Updated hints text."
    assert updated.successful_parses == before_parses
    assert updated.last_seen == before_last_seen


def test_set_hints_empty_string_clears_hints() -> None:
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Chase", hints="Initial hints.")
    cleared = repo.set_hints(bank_name="Chase", hints="")
    assert cleared.extraction_hints is None


def test_list_all_newest_seen_first_nulls_last() -> None:
    repo = InMemoryBankLayoutRepository()
    repo.upsert_success(bank_name="Bank A", fingerprint={})
    # Primed-only row (no parses) — last_seen stays None.
    repo.set_hints(bank_name="Bank Primed", hints="ready")
    repo.upsert_success(bank_name="Bank B", fingerprint={})

    rows = repo.list_all()
    names = [r.bank_name for r in rows]
    # Bank B is most recent (last upsert) → first.
    # Bank A is older → second.
    # Bank Primed has last_seen=None → bottom.
    assert names == ["Bank B", "Bank A", "Bank Primed"]
