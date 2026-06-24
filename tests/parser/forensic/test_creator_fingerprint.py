"""Tests for ``aegis.parser.forensic.creator_fingerprint``.

No PDFs — the detector operates on the already-extracted ``/Creator``
+ ``/Producer`` strings and ``bank_name``, so the tests just pass
those values directly to ``analyze``.

Registry is grounded in real data sampled from 15 production-parsed
proceed-status statements on 2026-06-24:

* JPMorgan Chase Bank, N.A. — ``/Creator=""``, ``/Producer="OpenText
  Output Transformation Engine - 23.4.25"`` across 11 sampled docs.
* Bank of America, N.A. — ``/Creator="Bank of America"``, ``/Producer``
  carries a "TargetStream" tag.
* TD Bank, N.A. — ``/Creator=""``, ``/Producer="iLovePDF"`` (operator
  whitelisted iLovePDF for TD specifically per OP-4 sub-rule).

The tests below use those literal strings to exercise the matching
logic so a future registry-change that breaks Chase / BoA / TD
recognition is caught by the suite.

Coverage:

* Clean (real) producer string on Chase — /Creator empty, /Producer
  carries known-good → no flag (the Chase-pattern case).
* Real BoA creator → no flag.
* iLovePDF on TD → no flag (whitelist case).
* Editing-tool /Producer on a bank where that tool is NOT whitelisted
  → flag with the producer string as detected_creator.
* Editing-tool /Creator on a non-whitelisted bank → flag.
* Unknown bank → graceful no-flag.
* Both fields empty → no-flag.
* Case-insensitive matching for both fields.
* Registry-sanity invariants.
"""

from __future__ import annotations

from aegis.parser.forensic.creator_fingerprint import (
    KNOWN_CREATOR_PATTERNS,
    CreatorFingerprintResult,
    analyze,
)

# ---------------------------------------------------------------------
# Clean paths — known-good combinations don't flag
# ---------------------------------------------------------------------


def test_chase_producer_only_known_good_no_flag() -> None:
    """Chase pattern: /Creator empty, /Producer carries the OpenText
    identity. The detector must reach the second-step known-good check
    on /Producer alone and return no-flag — no editing tool found in
    OpenText, so the function actually short-circuits to null before
    even reaching the bank check. Either way: no flag."""
    result = analyze(
        pdf_creator="",
        pdf_producer="OpenText Output Transformation Engine - 23.4.25",
        bank_name="JPMorgan Chase Bank, N.A.",
    )

    assert isinstance(result, CreatorFingerprintResult)
    assert result.mismatch_detected is False


def test_boa_creator_known_good_no_flag() -> None:
    """BoA pattern: /Creator="Bank of America" matches the registry
    known-good directly. The /Creator string is not on the editing-
    tool list, so the function short-circuits to null before reaching
    the bank check — but the result is still no-flag, which is correct."""
    result = analyze(
        pdf_creator="Bank of America",
        pdf_producer="TargetStream StreamEDS rv1.7.161 for Bank of America",
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is False


def test_ilovepdf_on_td_whitelisted_no_flag() -> None:
    """iLovePDF is on _EDITING_TOOL_PATTERNS as a generic tampering
    signal AND is specifically whitelisted for TD Bank because the
    operator observed iLovePDF on legitimate TD statements (operator
    decision 2026-06-24).

    This is the path that DOES reach the late-return branch:
    editing_tool_match='ilovepdf', then the known-good check finds
    iLovePDF in TD's pattern list and returns no-flag.
    """
    result = analyze(
        pdf_creator="",
        pdf_producer="iLovePDF",
        bank_name="TD Bank, N.A.",
    )

    assert result.mismatch_detected is False
    assert result.editing_tool_match == "ilovepdf"
    assert result.detected_creator == "iLovePDF"
    assert "iLovePDF" in result.expected_patterns


def test_td_empty_creator_and_producer_no_flag() -> None:
    """One of the 3 sampled TD docs has both fields empty. The detector
    short-circuits on (not pdf_creator and not pdf_producer) → null
    result, no flag. Reproduces the third TD shape from the prod
    sample exactly so a future change that breaks this path is
    caught."""
    result = analyze(
        pdf_creator="",
        pdf_producer="",
        bank_name="TD Bank, N.A.",
    )

    assert result.mismatch_detected is False


# ---------------------------------------------------------------------
# Mismatch paths — editing tool fires AND no known-good match
# ---------------------------------------------------------------------


def test_pdflib_producer_on_boa_flags_mismatch() -> None:
    """A statement claiming to be from BoA carrying PDFlib in /Producer
    → flag. PDFlib isn't in BoA's known-good (which is the BoA literal
    + TargetStream); editing-tool match fires; mismatch_detected=True
    with the producer string captured."""
    result = analyze(
        pdf_creator="",
        pdf_producer="PDFlib+PDI 8.0.2p1 (Win64)",
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is True
    assert result.editing_tool_match == "pdflib"
    assert result.detected_creator == "PDFlib+PDI 8.0.2p1 (Win64)"
    # Expected patterns surface for triage context.
    assert "Bank of America" in result.expected_patterns


def test_itext_creator_on_chase_flags_mismatch() -> None:
    """iText in /Creator on a Chase statement → flag. Chase's known-
    good is OpenText only; iText is on the editing-tool list and isn't
    a Chase-legitimate tool, so the mismatch fires with iText
    captured."""
    result = analyze(
        pdf_creator="iText 2.1.7 by 1T3XT",
        pdf_producer="",
        bank_name="JPMorgan Chase Bank, N.A.",
    )

    assert result.mismatch_detected is True
    assert result.editing_tool_match == "itext"
    assert result.detected_creator == "iText 2.1.7 by 1T3XT"


def test_sejda_producer_on_td_flags_mismatch() -> None:
    """Sejda (not iLovePDF) on a TD statement still flags — the
    iLovePDF whitelist is specific, not a blanket exemption. Verifies
    the whitelist doesn't accidentally let other editing tools through."""
    result = analyze(
        pdf_creator="",
        pdf_producer="Sejda PDF Editor 7.0",
        bank_name="TD Bank, N.A.",
    )

    assert result.mismatch_detected is True
    assert result.editing_tool_match == "sejda"
    assert result.detected_creator == "Sejda PDF Editor 7.0"


def test_creator_priority_when_both_match_editing_tools() -> None:
    """When BOTH /Creator and /Producer match an editing tool,
    /Creator takes priority for the detected_source field so the
    dossier display reads naturally. (The mismatch fires either way.)"""
    result = analyze(
        pdf_creator="Foxit PhantomPDF 11.2",
        pdf_producer="iText 2.1.7",
        bank_name="JPMorgan Chase Bank, N.A.",
    )

    assert result.mismatch_detected is True
    # /Creator took priority for the detected string.
    assert result.editing_tool_match == "foxit"
    assert result.detected_creator == "Foxit PhantomPDF 11.2"


# ---------------------------------------------------------------------
# Graceful no-flag paths (unknown bank / empty inputs / non-tool)
# ---------------------------------------------------------------------


def test_unknown_bank_returns_null_result() -> None:
    """A bank not in ``KNOWN_CREATOR_PATTERNS`` → no flag even when an
    editing tool is present. The operator owns registry growth — we
    don't penalise banks the registry hasn't been taught."""
    assert "First Bank of Mars" not in KNOWN_CREATOR_PATTERNS

    result = analyze(
        pdf_creator="PDFlib+PDI 8.0.2p1",
        pdf_producer="",
        bank_name="First Bank of Mars",
    )

    assert result.mismatch_detected is False


def test_both_fields_empty_returns_null_result() -> None:
    """No /Creator AND no /Producer → no comparison possible → null."""
    result = analyze(
        pdf_creator=None,
        pdf_producer=None,
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is False
    assert result.editing_tool_match is None


def test_empty_bank_name_returns_null_result() -> None:
    """Extraction returned no bank_name (rare) → null result even when
    an editing tool is present in /Creator."""
    result = analyze(
        pdf_creator="PDFlib+PDI 8.0.2p1",
        pdf_producer="",
        bank_name=None,
    )

    assert result.mismatch_detected is False


def test_non_editing_tool_creator_no_flag() -> None:
    """A creator that's neither editing-tool nor known-good (a future
    new in-house bank export tool not yet on either list) → no flag.
    The detector intentionally targets the editing-tool false-positive
    refinement and does NOT flag every unfamiliar creator."""
    result = analyze(
        pdf_creator="BankXYZ Internal Export 1.0",
        pdf_producer="",
        bank_name="Bank of America, N.A.",
    )

    assert result.mismatch_detected is False
    assert result.editing_tool_match is None


def test_case_insensitive_matching() -> None:
    """Substring match is case-insensitive — operator-facing registry
    entries can use natural capitalization and still match wire values
    in any case."""
    result = analyze(
        pdf_creator="bank of america",  # lowercase
        pdf_producer="",
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


def test_registry_only_contains_observed_banks() -> None:
    """The 2026-06-24 ground-truth pull only observed three banks
    with usable creator/producer strings: BoA, Chase, TD. Third Coast
    Bank had a placeholder entry seeded from industry guessing
    (CLAUDE.md OP-4 violation) and was removed. This test pins the
    deliberate scope so a future re-introduction of an
    industry-typical placeholder is caught.
    """
    assert "Third Coast Bank" not in KNOWN_CREATOR_PATTERNS
    # The four registered keys are the BoA pair + Chase + TD.
    assert set(KNOWN_CREATOR_PATTERNS.keys()) == {
        "Bank of America",
        "Bank of America, N.A.",
        "JPMorgan Chase Bank, N.A.",
        "TD Bank, N.A.",
    }
