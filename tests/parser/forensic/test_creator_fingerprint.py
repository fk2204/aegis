"""Tests for ``aegis.parser.forensic.creator_fingerprint``.

No PDFs — the detector operates on the already-extracted ``/Creator``
string + ``bank_name``, so the tests just pass those values directly
to ``analyze``.

Coverage:

* Clean creator (Adobe PDF Library) on Bank of America — bank IS in
  the registry, creator matches BoA's known-good pattern → no flag.
* Editing-tool creator (PDFlib) on Bank of America — bank IS in the
  registry, creator matches editing-tool family but NOT BoA's
  known-good → flag, editing_tool_match populated, expected_patterns
  exposed for triage context.
* Editing-tool creator on an unknown bank ("First Bank of Mars") →
  graceful no-flag, registry doesn't penalise banks it hasn't been
  taught.
* Missing bank_name / creator → no-flag (can't compare).
* Editing-tool creator on a bank with iText explicitly NOT in known-
  good → flag (the case the operator's spec called out: PDFlib on a
  BoA statement).
"""

from __future__ import annotations

from aegis.parser.forensic.creator_fingerprint import (
    KNOWN_CREATOR_PATTERNS,
    CreatorFingerprintResult,
    analyze,
)

# ---------------------------------------------------------------------
# Clean-creator paths (no flag)
# ---------------------------------------------------------------------


def test_clean_boa_creator_no_flag() -> None:
    """Adobe PDF Library on BoA — NOT on the editing-tools list (not a
    tampering surface), so the detector short-circuits to null. The
    'clean BoA' contract is correctly satisfied by the early return:
    no editing tool detected = no fingerprinting concern."""
    result = analyze(
        pdf_creator="Adobe PDF Library 15.0",
        bank_name="Bank of America, N.A.",
    )

    assert isinstance(result, CreatorFingerprintResult)
    assert result.mismatch_detected is False
    # Early-return path: detected_creator left empty because we never
    # made it past the editing-tool check.
    assert result.detected_creator == ""
    assert result.editing_tool_match is None


def test_editing_tool_creator_in_chase_known_good_no_flag() -> None:
    """Adobe Acrobat 9.5.5 on Chase — IS on the editing-tool list AND
    matches Chase's known-good 'Adobe Acrobat' substring. Reaches the
    full comparison path and returns no-mismatch with the creator +
    expected patterns captured for operator context."""
    result = analyze(
        pdf_creator="Adobe Acrobat 9.5.5",
        bank_name="JPMorgan Chase Bank, N.A.",
    )

    assert result.mismatch_detected is False
    assert result.detected_creator == "Adobe Acrobat 9.5.5"
    assert result.editing_tool_match == "adobe acrobat"
    assert "Adobe Acrobat" in result.expected_patterns


# ---------------------------------------------------------------------
# Mismatch paths (flag)
# ---------------------------------------------------------------------


def test_pdflib_creator_on_boa_statement_flags_mismatch() -> None:
    """Operator-spec example: PDFlib creator on a BoA statement →
    editing-tool match, doesn't match BoA's known-good ('Adobe PDF
    Library'), so the mismatch flag fires with full context."""
    result = analyze(
        pdf_creator="PDFlib+PDI 8.0.2p1 (Win64)",
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is True
    assert result.detected_creator == "PDFlib+PDI 8.0.2p1 (Win64)"
    assert result.editing_tool_match == "pdflib"
    # Expected-patterns surfaces for operator triage so they can see
    # WHY this is a mismatch ("we expected Adobe PDF Library").
    assert "Adobe PDF Library" in result.expected_patterns


def test_itext_creator_on_chase_statement_flags_mismatch() -> None:
    """iText on Chase — Chase known-good is Adobe-family only, so
    iText fires the mismatch."""
    result = analyze(
        pdf_creator="iText 2.1.7 by 1T3XT",
        bank_name="JPMorgan Chase Bank, N.A.",
    )

    assert result.mismatch_detected is True
    assert result.editing_tool_match == "itext"


# ---------------------------------------------------------------------
# Graceful no-flag paths
# ---------------------------------------------------------------------


def test_unknown_bank_returns_null_result() -> None:
    """A bank not in ``KNOWN_CREATOR_PATTERNS`` → no flag even when the
    creator matches an editing-tool. The registry's growth is
    operator-owned; we don't penalise banks it hasn't been taught."""
    assert "First Bank of Mars" not in KNOWN_CREATOR_PATTERNS

    result = analyze(
        pdf_creator="PDFlib+PDI 8.0.2p1",
        bank_name="First Bank of Mars",
    )

    assert result.mismatch_detected is False


def test_empty_creator_returns_null_result() -> None:
    """No /Creator string → no comparison possible → null result."""
    result = analyze(
        pdf_creator=None,
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is False
    assert result.editing_tool_match is None


def test_empty_bank_name_returns_null_result() -> None:
    """Extraction returned no bank_name (rare) → null result."""
    result = analyze(
        pdf_creator="PDFlib+PDI 8.0.2p1",
        bank_name=None,
    )

    assert result.mismatch_detected is False


def test_non_editing_tool_creator_no_flag() -> None:
    """A creator that's neither editing-tool nor known-good (e.g. a
    new in-house bank export tool not yet on either list) → no flag.
    The detector intentionally targets the editing-tool false-positive
    refinement and does NOT flag every unfamiliar creator."""
    result = analyze(
        pdf_creator="BankXYZ Internal Export 1.0",
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is False
    assert result.editing_tool_match is None


def test_case_insensitive_matching() -> None:
    """Substring match is case-insensitive — operator-facing registry
    entries can use natural capitalization and still match wire values
    in any case."""
    result = analyze(
        pdf_creator="adobe pdf library 17",  # lowercase
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is False


# ---------------------------------------------------------------------
# Registry sanity checks (small invariants worth locking)
# ---------------------------------------------------------------------


def test_boa_short_and_long_name_both_in_registry() -> None:
    """The LLM emits both ``Bank of America`` and ``Bank of America,
    N.A.`` depending on which page the bank-name token came from. Both
    must be registered or one variant slips through ungated."""
    assert "Bank of America" in KNOWN_CREATOR_PATTERNS
    assert "Bank of America, N.A." in KNOWN_CREATOR_PATTERNS
    # Same expected patterns under both keys to keep the gate uniform.
    assert (
        KNOWN_CREATOR_PATTERNS["Bank of America"] == KNOWN_CREATOR_PATTERNS["Bank of America, N.A."]
    )


def test_registry_values_are_nonempty_lists() -> None:
    """Empty-list registry entries cause graceful no-flag (per docstring)
    but indicate the operator hasn't yet seeded the bank. The current
    seed should have at least one pattern per registered bank — empty
    lists are operator surface debt to flag for follow-up."""
    for bank_name, patterns in KNOWN_CREATOR_PATTERNS.items():
        assert patterns, f"Empty known-good patterns for {bank_name!r}"
