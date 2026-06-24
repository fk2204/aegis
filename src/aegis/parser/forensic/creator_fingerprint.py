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
invokes ``analyze(pdf_creator, pdf_producer, bank_name)`` post-
extraction and folds the result into
``MetadataAnalysis.creator_mismatch_detected`` plus the document's
``all_flags``.

Why both /Creator AND /Producer
--------------------------------
Empirical: of 15 known-good production statements sampled 2026-06-24,
11 (Chase) had ``/Creator=""`` with the bank identity living on
``/Producer="OpenText Output Transformation Engine"``. A /Creator-only
detector misses that entire class of legitimate statements. We
check both fields against the per-bank known-good patterns — a match
on EITHER means no flag. Editing-tool detection also scans both.

Registry: ``KNOWN_CREATOR_PATTERNS``
------------------------------------
A dict mapping bank names (matching the strings the parser emits as
``StatementSummary.bank_name``) to a list of known-good substrings
that appear in either ``/Creator`` or ``/Producer``. Case-insensitive
substring matching — the registry stores the recognisable fragment
("OpenText Output Transformation Engine") and we match it against
the full field value.

The current registry was seeded 2026-06-24 from 15 production-parsed
proceed-status statements. Every entry below was OBSERVED on a real
statement; the historical "Adobe family" placeholder seed was wrong
across all four banks and has been replaced. Continue to grow this
registry from real data, not industry-typical guesses (CLAUDE.md
operating-principle #4).

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


# Per-bank known-good substrings. Case-insensitive substring match
# against the full ``/Creator`` value OR the ``/Producer`` value (a
# match on either field suffices).
#
# Patterns below were extracted from 15 production-parsed proceed-
# status statements on 2026-06-24 (samples sizes annotated per bank).
# Operator's call: real data first, no industry-typical placeholders
# (CLAUDE.md OP-4).
KNOWN_CREATOR_PATTERNS: Final[dict[str, list[str]]] = {
    # JPMorgan Chase — n=16 sampled statements (11 proceed + 5
    # manual_review, all identical strings). /Creator is empty; the
    # bank identity lives on /Producer="OpenText Output Transformation
    # Engine - 23.4.25". Highest-confidence entry in this registry.
    "JPMorgan Chase Bank, N.A.": [
        "OpenText Output Transformation Engine",
    ],
    # Bank of America — n=5 sampled statements (1 proceed + 3
    # manual_review under "Bank of America, N.A." + 1 manual_review
    # under "Bank of America" short variant). All five carry the same
    # /Creator="Bank of America" + /Producer="TargetStream StreamEDS
    # rv1.7.161 for Bank of America". Both name variants the LLM emits
    # get the same patterns to keep the gate uniform. Upgraded from
    # preliminary (n=1) on the 2026-06-24 wider-scope sample.
    "Bank of America, N.A.": [
        "Bank of America",
        "TargetStream",
    ],
    "Bank of America": [
        "Bank of America",
        "TargetStream",
    ],
    # TD Bank — n=3 sampled statements. 2 carry /Producer="iLovePDF";
    # 1 carries empty creator + empty producer (the detector returns
    # null in that case so it doesn't matter for false-positive risk).
    # iLovePDF is on _EDITING_TOOL_PATTERNS as a generic tampering
    # signal — operator's call (2026-06-24) was to whitelist it for
    # TD specifically rather than strip it from the tool list globally.
    # The whitelist works because the editing-tool match runs FIRST
    # (giving editing_tool_match='ilovepdf'), then the known-good check
    # finds 'iLovePDF' in the bank's pattern list and returns the
    # no-flag result.
    "TD Bank, N.A.": [
        "iLovePDF",
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
    pdf_producer: str | None,
    bank_name: str | None,
) -> CreatorFingerprintResult:
    """Compare a PDF's ``/Creator`` + ``/Producer`` against a bank's
    known-good profile.

    Returns the null result (no flag) when:
      * Both ``pdf_creator`` and ``pdf_producer`` are None / empty —
        nothing to compare.
      * ``bank_name`` is None / empty — can't look up known-good
        patterns.
      * ``bank_name`` not in ``KNOWN_CREATOR_PATTERNS`` — unknown bank
        gets graceful no-flag (don't penalise banks the registry hasn't
        been taught).
      * Neither field matches any editing-tool pattern in
        ``_EDITING_TOOL_PATTERNS`` — no tampering surface to corroborate.

    Returns a mismatch result (``mismatch_detected=True``) only when:
      * Either field matches a known editing-tool family, AND
      * NEITHER field matches any of the bank's known-good patterns.

    Case-insensitive substring matching throughout — bank exports
    typically embed full version strings whose stable identifying
    fragment ("OpenText Output Transformation Engine") is what we
    register.

    The ``detected_creator`` field on the returned result is populated
    with whichever of the two source fields actually carried the
    editing-tool match (priority: /Creator first, then /Producer
    fallback). That string is what the dossier surfaces to the
    underwriter so they see the actual evidence.
    """
    if not pdf_creator and not pdf_producer:
        return _NULL_RESULT
    if not bank_name:
        return _NULL_RESULT

    expected_patterns = KNOWN_CREATOR_PATTERNS.get(bank_name)
    if expected_patterns is None:
        # Unknown bank — graceful no-flag.
        return _NULL_RESULT

    creator_lower = (pdf_creator or "").lower()
    producer_lower = (pdf_producer or "").lower()

    # First: does either field look like an editing tool? If not,
    # there's no tampering signal to refine. Some bank exports use
    # in-house tools whose names aren't on the editing-tool list; those
    # land here as no-flag, which is correct. Scan /Creator FIRST in
    # its entirety, then /Producer — so a creator-side match takes
    # priority over a producer-side match when both fields carry tool
    # names. Within each source, the iteration order of
    # ``_EDITING_TOOL_PATTERNS`` decides ties (first match wins).
    editing_tool_match: str | None = None
    detected_source: str = ""
    if creator_lower:
        for tool in _EDITING_TOOL_PATTERNS:
            if tool in creator_lower:
                editing_tool_match = tool
                detected_source = pdf_creator or ""
                break
    if editing_tool_match is None and producer_lower:
        for tool in _EDITING_TOOL_PATTERNS:
            if tool in producer_lower:
                editing_tool_match = tool
                detected_source = pdf_producer or ""
                break

    if editing_tool_match is None:
        return _NULL_RESULT

    # Second: does either field match any of the bank's known-good
    # patterns? If yes, the editing-tool detection is a false positive
    # in this bank's context — return no-flag. (The iLovePDF-on-TD
    # whitelist works through this branch.)
    for pattern in expected_patterns:
        pattern_lower = pattern.lower()
        if (creator_lower and pattern_lower in creator_lower) or (
            producer_lower and pattern_lower in producer_lower
        ):
            return CreatorFingerprintResult(
                mismatch_detected=False,
                detected_creator=detected_source,
                expected_patterns=list(expected_patterns),
                editing_tool_match=editing_tool_match,
            )

    # Editing tool detected AND neither field matches any bank known-
    # good → flag. This is the stronger signal — corroborates the
    # ``editor_detected`` metadata flag with bank-context evidence.
    return CreatorFingerprintResult(
        mismatch_detected=True,
        detected_creator=detected_source,
        expected_patterns=list(expected_patterns),
        editing_tool_match=editing_tool_match,
    )


__all__ = [
    "KNOWN_CREATOR_PATTERNS",
    "CreatorFingerprintResult",
    "analyze",
]
