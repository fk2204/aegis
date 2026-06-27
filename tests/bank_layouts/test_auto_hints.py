"""Tests for ``aegis.bank_layouts.auto_hints``.

Synthetic first_page_text inputs (documented as such — no real
captured statement text lives in these fixtures). The strings are
hand-crafted to exercise the regex / label probes the generator runs.
The test discipline rule (real captured fixtures) applies to fixtures
that feed external APIs; this generator is a pure-Python text walker
so the synthetic strings are appropriate here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aegis.bank_layouts.auto_hints import generate_hints_from_parse_result, merge_hints
from aegis.bank_layouts.repository import (
    AUTO_HINTS_AVAILABLE_THRESHOLD,
    HINTS_AVAILABLE_THRESHOLD,
    InMemoryBankLayoutRepository,
)


@dataclass
class _FakeTransaction:
    """Minimal duck-typed transaction shape with optional running_balance."""

    running_balance: Decimal | None = None


@dataclass
class _FakeClassified:
    transactions: list[_FakeTransaction]


@dataclass
class _FakeParseResult:
    classified: _FakeClassified
    extraction: Any = None


_CHASE_FIRST_PAGE = (
    "JPMorgan Chase Bank, N.A.\n"
    "P O Box 182051\n"
    "Columbus, OH 43218-2051\n"
    "\n"
    "CHECKING SUMMARY\n"
    "Beginning Balance       1,234.56\n"
    "Deposits and Additions   500.00\n"
    "Checks Paid              -50.00\n"
    "Electronic Withdrawals  -200.00\n"
    "Ending Balance         1,484.56\n"
    "\n"
    "Statement Period: January 1, 2026 through January 31, 2026\n"
    "\n"
    "Date Description Amount\n"
    "01/05 Direct Deposit 500.00\n"
)


def test_generate_hints_from_parse_result_returns_non_empty_for_realistic_input() -> None:
    """Chase-shaped first page → hint mentions period / summary / header / bank."""
    parse_result = _FakeParseResult(
        classified=_FakeClassified(
            transactions=[_FakeTransaction(running_balance=None) for _ in range(8)]
        )
    )
    hint = generate_hints_from_parse_result(
        bank_name="JPMorgan Chase Bank, N.A.",
        first_page_text=_CHASE_FIRST_PAGE,
        parse_result=parse_result,
    )
    assert hint, "expected non-empty hint for the Chase-shaped fixture"
    assert "Period formatted as" in hint
    assert "through" in hint  # captured 'Month DD, YYYY through ...' pattern
    assert "Summary block uses labels" in hint
    assert "'Beginning Balance'" in hint
    assert "'Ending Balance'" in hint
    assert "Transaction section header" in hint
    assert "Bank identifier 'JPMorgan Chase Bank, N.A.'" in hint
    # No running balance → the no-running-balance sentence fires.
    assert "do NOT include a per-row running-balance column" in hint


def test_generate_hints_includes_running_balance_sentence_when_present() -> None:
    """When any transaction carries a running_balance, the hint says so."""
    parse_result = _FakeParseResult(
        classified=_FakeClassified(
            transactions=[
                _FakeTransaction(running_balance=Decimal("1.00")),
                _FakeTransaction(running_balance=None),
            ]
        )
    )
    hint = generate_hints_from_parse_result(
        bank_name="JPMorgan Chase Bank, N.A.",
        first_page_text=_CHASE_FIRST_PAGE,
        parse_result=parse_result,
    )
    assert "include a per-row running-balance column" in hint
    assert "do NOT" not in hint  # ensure we're not emitting the wrong branch


def test_generate_hints_returns_empty_string_for_empty_first_page() -> None:
    """Empty first-page text → empty hint, no error."""
    parse_result = _FakeParseResult(classified=_FakeClassified(transactions=[]))
    assert (
        generate_hints_from_parse_result(
            bank_name="Anything",
            first_page_text="",
            parse_result=parse_result,
        )
        == ""
    )
    assert (
        generate_hints_from_parse_result(
            bank_name="Anything",
            first_page_text="   \n\t   ",
            parse_result=parse_result,
        )
        == ""
    )


def test_merge_hints_does_not_duplicate_period_format() -> None:
    """Re-running the generator over the same input doesn't bloat the row."""
    parse_result = _FakeParseResult(classified=_FakeClassified(transactions=[]))
    first = generate_hints_from_parse_result(
        bank_name="JPMorgan Chase Bank, N.A.",
        first_page_text=_CHASE_FIRST_PAGE,
        parse_result=parse_result,
    )
    # Merge with itself — should produce IDENTICAL text (no duplicate).
    merged = merge_hints(first, first)
    assert merged == first
    # And merge_hints is idempotent — re-running again is the same fixed point.
    assert merge_hints(merged, first) == first


def test_merge_hints_handles_none_existing() -> None:
    """merge_hints(None, x) returns x verbatim."""
    assert merge_hints(None, "Sentence one. Sentence two.") == "Sentence one. Sentence two."
    assert merge_hints("", "Sentence one.") == "Sentence one."


def test_merge_hints_handles_empty_new() -> None:
    """merge_hints(existing, '') returns existing verbatim (no-op merge)."""
    existing = "Sentence one. Sentence two."
    assert merge_hints(existing, "") == existing
    assert merge_hints(existing, "   ") == existing


def test_merge_hints_appends_only_novel_sentences() -> None:
    """New sentences not in existing get appended; overlapping sentences are skipped."""
    existing = "Sentence A. Sentence B."
    new = "Sentence B. Sentence C."  # B overlaps, C is novel
    merged = merge_hints(existing, new)
    assert "Sentence A." in merged
    assert "Sentence B." in merged
    assert "Sentence C." in merged
    # Sentence B must not appear twice.
    assert merged.count("Sentence B.") == 1


def test_get_hints_threshold_respects_source_auto_vs_manual() -> None:
    """source='auto' returns hints at parses=1; source='manual' requires 3."""
    repo = InMemoryBankLayoutRepository()
    # AUTO branch: parses=1 is sufficient.
    repo.set_hints(bank_name="Auto Bank", hints="Auto hint sentence.", source="auto")
    for _ in range(AUTO_HINTS_AVAILABLE_THRESHOLD):
        repo.upsert_success(bank_name="Auto Bank", fingerprint={})
    assert repo.get_hints("Auto Bank") == "Auto hint sentence."
    # MANUAL branch: parses=1 is NOT sufficient.
    repo.set_hints(bank_name="Manual Bank", hints="Manual hint sentence.", source="manual")
    repo.upsert_success(bank_name="Manual Bank", fingerprint={})
    assert repo.get_hints("Manual Bank") is None
    # Bump manual to threshold → now returns hints.
    for _ in range(HINTS_AVAILABLE_THRESHOLD - 1):
        repo.upsert_success(bank_name="Manual Bank", fingerprint={})
    assert repo.get_hints("Manual Bank") == "Manual hint sentence."


def test_set_hints_auto_then_manual_promotes_to_mixed() -> None:
    """Writing auto then manual upgrades hints_source to 'mixed' + uses manual threshold."""
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Mixed Bank", hints="Auto hint.", source="auto")
    row = repo.find_by_bank_name("Mixed Bank")
    assert row is not None and row.hints_source == "auto"
    # Manual write follows — upgrade to 'mixed'.
    repo.set_hints(bank_name="Mixed Bank", hints="Manual hint.", source="manual")
    row = repo.find_by_bank_name("Mixed Bank")
    assert row is not None and row.hints_source == "mixed"
    # 'mixed' uses the conservative (manual) threshold, so parses=1 is below.
    repo.upsert_success(bank_name="Mixed Bank", fingerprint={})
    assert repo.get_hints("Mixed Bank") is None
    # Bump to manual threshold → readable.
    for _ in range(HINTS_AVAILABLE_THRESHOLD - 1):
        repo.upsert_success(bank_name="Mixed Bank", fingerprint={})
    assert repo.get_hints("Mixed Bank") == "Manual hint."


def test_set_hints_manual_then_auto_also_promotes_to_mixed() -> None:
    """Reverse direction: manual first, then auto, still upgrades to 'mixed'."""
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Reverse Bank", hints="Manual hint.", source="manual")
    repo.set_hints(bank_name="Reverse Bank", hints="Auto hint.", source="auto")
    row = repo.find_by_bank_name("Reverse Bank")
    assert row is not None and row.hints_source == "mixed"


def test_get_raw_hints_returns_hints_regardless_of_threshold() -> None:
    """get_raw_hints bypasses the threshold gate (used by merge path)."""
    repo = InMemoryBankLayoutRepository()
    # Manual hint, parses=0 — below threshold, get_hints returns None.
    repo.set_hints(bank_name="Sparse Bank", hints="Sparse hint.", source="manual")
    assert repo.get_hints("Sparse Bank") is None
    # But get_raw_hints surfaces it for the merger.
    assert repo.get_raw_hints("Sparse Bank") == "Sparse hint."
    # Unknown bank → None.
    assert repo.get_raw_hints("Never Heard Of It") is None


def test_set_hints_default_source_is_manual() -> None:
    """Backward compat: existing operator-UI callers don't pass source."""
    repo = InMemoryBankLayoutRepository()
    repo.set_hints(bank_name="Default Bank", hints="Operator hint.")
    row = repo.find_by_bank_name("Default Bank")
    assert row is not None
    assert row.hints_source == "manual"
