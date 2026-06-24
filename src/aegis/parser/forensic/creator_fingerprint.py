"""Per-bank PDF-creator fingerprinting detector.

Refines the existing ``[META] editor_detected`` signal in ``metadata.py``
with bank-context. The metadata-layer flag fires on ANY edit-tool
creator string (``iText``, ``PDFlib``, ``iLovePDF``, etc.) — a generic
signal that doesn't know whether a given tool is legitimate for a given
bank's exports. This detector adds the missing context: when a statement
identifies as bank X and the creator matches an editing-tool family that
is NOT on bank X's known-good list, surface the additional
``[META] creator_mismatch_detected`` flag.

The combined signal (existing ``editor_detected`` + new
``creator_mismatch_detected``) is stronger than ``editor_detected``
alone — it differentiates "iText present, but legitimate for this
bank" (e.g. if a bank's own export system uses iText) from "iText
present and the bank is known to NOT use it" (which is a tamper signal
much closer to ground truth).

Runtime contract
----------------
The check needs the bank_name from the extraction pass, which runs
AFTER the metadata layer. The caller (``parser.pipeline.run_pipeline``)
invokes ``analyze(pdf_creator, bank_name)`` post-extraction and folds
the result into ``MetadataAnalysis.creator_mismatch_detected`` plus the
document's ``all_flags``.

Registry: ``KNOWN_CREATOR_PATTERNS``
------------------------------------
A dict mapping bank names (matching the strings the parser emits as
``StatementSummary.bank_name``) to a list of known-good creator-string
SUBSTRINGS. Case-insensitive substring matching — the registry stores
the recognisable fragment ("Adobe PDF Library") and we match it
against the full ``/Creator`` (e.g. "Adobe PDF Library 15.0").

Seed entries below are based on plausible export profiles for the
banks present in AEGIS's current corpus. They MUST be verified by the
operator against real statements during the first weeks after this
detector ships — getting a known-good wrong here creates false
positives (clean statements flagged as mismatches). Easy way to verify:
parse a real bank-issued statement and inspect the resulting
``MetadataAnalysis.pdf_creator`` field; that's the ground truth for
"what does Chase actually produce."

Unknown-bank fall-through
-------------------------
When the identified bank has no entry in ``KNOWN_CREATOR_PATTERNS``,
the detector returns a null result instead of flagging. Penalising
banks the registry hasn't been taught yet would be a false-positive
factory — the operator owns the registry's growth, not the parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ─────────────────────────────────────────────────────────────────────
# Registry: per-bank known-good creator-string substrings
# ─────────────────────────────────────────────────────────────────────


# Editor families generally associated with PDF tampering / re-encoding.
# Subset of ``metadata._HARD_EDITORS`` / ``_MEDIUM_EDITORS`` — we only
# need to recognise the family name to ask the "is this expected for
# the identified bank?" question. Adding a tool here without adding
# it to the metadata-layer hard/medium editors list is harmless;
# removing one would just shrink this detector's surface.
_EDITING_TOOL_PATTERNS: Final[tuple[str, ...]] = (
    "pdflib",
    "ilovepdf",
    "itext",
    "smallpdf",
    "sejda",
    "foxit",
    "pdf-xchange",
    "nitro pro",
    "pdfescape",
    "cutepdf",
    "pdfill",
    "ghostscript",
    "pypdf",
    "adobe acrobat",
    "preview",  # macOS Preview — re-saves often happen here
    "microsoft word",
    "libreoffice",
    "google docs",
)


# Per-bank known-good creator-string substrings. Case-insensitive
# substring match against the full ``/Creator`` value.
#
# 🛑 OPERATOR ACTION (post-deploy): verify each entry against real
# statements. The seed values below are plausible-but-unverified. To
# verify: parse a known-clean statement from each bank, inspect
# ``MetadataAnalysis.pdf_creator`` in the resulting document row,
# update this dict to match. Wrong entries here cause false positives
# on legitimate statements; an EMPTY list for a bank (or no entry at
# all) causes graceful no-flag, NOT a false positive.
KNOWN_CREATOR_PATTERNS: Final[dict[str, list[str]]] = {
    # JPMorgan Chase — corpus is well-populated. Operator to confirm
    # Chase's actual /Creator string and update.
    "JPMorgan Chase Bank, N.A.": [
        "Adobe PDF Library",
        "Adobe Acrobat",
    ],
    # Bank of America — both name variants the LLM emits. iText IS
    # generally a tampering signal (in ``_HARD_EDITORS``), but if BoA's
    # own export system uses iText that creates a false positive on
    # every BoA statement. Verify against a clean BoA export before
    # populating; for now we only register the Adobe family.
    "Bank of America, N.A.": [
        "Adobe PDF Library",
    ],
    "Bank of America": [
        "Adobe PDF Library",
    ],
    # TD Bank — operator to verify.
    "TD Bank, N.A.": [
        "Adobe PDF Library",
    ],
    # Third Coast Bank — operator to verify.
    "Third Coast Bank": [
        "Adobe PDF Library",
    ],
}


# ─────────────────────────────────────────────────────────────────────
# Result shape
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreatorFingerprintResult:
    """Document-level rollup for the creator-fingerprint detector.

    * ``mismatch_detected`` is the gate the caller in
      ``parser.pipeline.run_pipeline`` reads to decide whether to
      surface ``[META] creator_mismatch_detected`` on ``all_flags``.
    * ``detected_creator`` is the raw ``/Creator`` string we evaluated.
      Captured so the operator's downstream triage view has the actual
      value, not just "mismatch".
    * ``expected_patterns`` is the known-good substring list for the
      identified bank. Surfaced for context in the audit / dossier
      layer — useful when triaging a flag to say "we expected X, saw
      Y."
    * ``editing_tool_match`` names the editing-tool family the
      creator matched (the reason we cared in the first place). None
      when no editing tool matched — in that case ``mismatch_detected``
      is False trivially.
    """

    mismatch_detected: bool
    detected_creator: str
    expected_patterns: list[str]
    editing_tool_match: str | None


# Conservative no-signal default used by every "skip the check" branch.
_NULL_RESULT: Final[CreatorFingerprintResult] = CreatorFingerprintResult(
    mismatch_detected=False,
    detected_creator="",
    expected_patterns=[],
    editing_tool_match=None,
)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def analyze(
    pdf_creator: str | None,
    bank_name: str | None,
) -> CreatorFingerprintResult:
    """Compare a PDF's ``/Creator`` against a bank's known-good profile.

    Returns the null result (no flag) when:
      * ``pdf_creator`` is None / empty — nothing to compare.
      * ``bank_name`` is None / empty — can't look up known-good
        patterns.
      * ``bank_name`` not in ``KNOWN_CREATOR_PATTERNS`` — unknown bank
        gets graceful no-flag (don't penalise banks the registry hasn't
        been taught).
      * Creator string does NOT match any editing-tool pattern in
        ``_EDITING_TOOL_PATTERNS`` — no tampering surface to corroborate.

    Returns a mismatch result (``mismatch_detected=True``) only when:
      * The creator matches a known editing-tool family, AND
      * The creator does NOT match any of the bank's known-good
        patterns.

    Case-insensitive substring matching throughout — bank exports
    typically embed full version strings ("Adobe PDF Library 15.0")
    whose stable identifying fragment ("Adobe PDF Library") is what
    we register.
    """
    if not pdf_creator:
        return _NULL_RESULT
    if not bank_name:
        return _NULL_RESULT

    expected_patterns = KNOWN_CREATOR_PATTERNS.get(bank_name)
    if expected_patterns is None:
        # Unknown bank — graceful no-flag.
        return _NULL_RESULT

    creator_lower = pdf_creator.lower()

    # First: does the creator look like an editing tool at all? If not,
    # there's no tampering signal to refine. Some bank exports use
    # in-house tools whose names aren't on the editing-tool list; those
    # land here as no-flag, which is correct.
    editing_tool_match: str | None = None
    for tool in _EDITING_TOOL_PATTERNS:
        if tool in creator_lower:
            editing_tool_match = tool
            break

    if editing_tool_match is None:
        return _NULL_RESULT

    # Second: does the creator match any of the bank's known-good
    # patterns? If yes, the editing-tool detection is a false positive
    # in this bank's context — return no-flag.
    for pattern in expected_patterns:
        if pattern.lower() in creator_lower:
            return CreatorFingerprintResult(
                mismatch_detected=False,
                detected_creator=pdf_creator,
                expected_patterns=list(expected_patterns),
                editing_tool_match=editing_tool_match,
            )

    # Editing tool detected AND doesn't match any bank known-good →
    # flag. This is the stronger signal — corroborates the
    # ``editor_detected`` metadata flag with bank-context evidence.
    return CreatorFingerprintResult(
        mismatch_detected=True,
        detected_creator=pdf_creator,
        expected_patterns=list(expected_patterns),
        editing_tool_match=editing_tool_match,
    )


__all__ = [
    "KNOWN_CREATOR_PATTERNS",
    "CreatorFingerprintResult",
    "analyze",
]
