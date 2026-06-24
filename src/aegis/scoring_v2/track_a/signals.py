"""Helpers for extracting structured signals from raw parser flags.

Track A's input is a bag of strings the existing parser already
produces. The strings carry well-known prefixes (``editor_detected:``,
``reconciliation_failed_*:``, ``future_dated_*:``); these helpers
project them into the structured signals the verdict logic consumes.

The prefix taxonomy mirrors what ``aegis.parser.metadata`` and
``aegis.parser.validate`` emit. Any new flag class added there
must add a recognition pattern here AND a unit test against a real
captured flag string — same CLAUDE.md "external-integration test
discipline" rule the rest of the redesign follows.
"""

from __future__ import annotations

import re
from typing import Final

from aegis.scoring_v2.track_a.models import EvidenceItem

# Failure-code prefixes that count as drift. The reconciliation_failed
# family is broad — running_balance, period, deposit_total,
# withdrawal_total all reflect the same "the math doesn't add up"
# story. ``future_dated_*`` is grouped here because a future-dated
# statement is the parallel structural anomaly the
# ``aegis.parser.tampering`` rule already treats as corroboration.
DRIFT_FAILURE_PREFIXES: Final[tuple[str, ...]] = (
    "reconciliation_failed",
    "future_dated",
)


# Metadata-flag prefixes Track A recognises. The ``editor_detected:``
# prefix is the load-bearing one — its presence (regardless of
# metadata_score) gates the drift_plus_editor → fail branch.
_EDITOR_FLAG_PREFIX: Final[str] = "editor_detected:"


# Persistence-time category prefix the storage layer prepends to flag
# strings before writing them to ``DocumentRow.all_flags`` /
# ``metadata_flags`` (e.g. ``[MATH] reconciliation_failed_period: …``
# becomes the canonical form once the doc row lands). The raw parser
# emits the unprefixed form (``reconciliation_failed_period: …``);
# Track A tolerates both formats so callers can pass flags from
# either the in-memory ValidationResult OR the persisted DB column
# without remembering which.
_CATEGORY_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[(META|MATH|PATTERN|STRUCT|OCR|LLM|OFAC)\]\s+"
)


def _strip_category_prefix(flag: str) -> str:
    """Remove the ``[META] `` / ``[MATH] `` / etc. category prefix
    if present. Idempotent: a raw parser flag passes through
    unchanged."""
    return _CATEGORY_PREFIX_RE.sub("", flag)


def extract_drift_failures(
    validation_failures: tuple[str, ...],
) -> tuple[str, ...]:
    """Return only the failures that count as math/structural drift.

    Excludes behavioural-pattern failures (concentration, payroll,
    etc.) — those belong to Track B / Track C, not Track A. Mirrors
    ``aegis.parser.tampering._CORROBORATING_PREFIXES``.

    Tolerates either the raw parser format (``reconciliation_failed_…``)
    or the persisted storage format (``[MATH] reconciliation_failed_…``)
    by stripping the category prefix before matching.
    """
    return tuple(
        f
        for f in validation_failures
        if _strip_category_prefix(f).startswith(DRIFT_FAILURE_PREFIXES)
    )


def extract_editor_metadata_flag(
    metadata_flags: tuple[str, ...],
    validation_failures: tuple[str, ...] = (),
) -> str | None:
    """Return the literal editor flag string if any, otherwise None.

    Returns only the FIRST one found — multiple ``editor_detected``
    flags on the same document are uncommon enough to not be a real
    signal differentiator. The presence is what matters.

    The returned string is normalised to the unprefixed parser form
    so downstream evidence renders consistently regardless of
    whether the caller passed flags from the in-memory ValidationResult
    or the persisted DB column.

    ``metadata_flags`` is the primary source (the parser writes editor
    detections there, and ``storage.py`` round-trips them on
    ``DocumentRow.metadata_flags``). When that source yields nothing,
    we fall back to scanning ``validation_failures`` for the same
    ``editor_detected:`` prefix — ``parser/pipeline.py::_collect_flags``
    also mirrors every metadata flag into ``all_flags`` with a
    ``[META] `` prefix, and ``dossier_panel._signals_for_document``
    populates Track A's ``validation_failures`` from ``all_flags``. The
    dual-source persistence means a future partial-update or
    re-parse race that drops ``metadata_flags`` but preserves
    ``all_flags`` (or vice versa) still surfaces the editor flag.

    Closes F4 in docs/track_a_audit_2026-06-12.md.
    """
    for f in metadata_flags:
        stripped = _strip_category_prefix(f)
        if stripped.startswith(_EDITOR_FLAG_PREFIX):
            return stripped
    for f in validation_failures:
        stripped = _strip_category_prefix(f)
        if stripped.startswith(_EDITOR_FLAG_PREFIX):
            return stripped
    return None


def extract_other_metadata_flags(
    metadata_flags: tuple[str, ...],
) -> tuple[str, ...]:
    """Return non-editor metadata flags. Useful for evidence rendering
    on strong-metadata fails where the underwriter wants to see
    everything that contributed to the score. Normalises the
    category prefix the same way ``extract_editor_metadata_flag``
    does.

    Excludes the three forensic-layer flags (``font_inconsistency_detected``,
    ``creator_mismatch_detected``, ``text_overlay_detected``) — those
    surface through ``extract_forensic_signals`` instead so the dossier
    can render them with their own plain-English rationale rather than
    as raw ``metadata_flag`` rows.
    """
    return tuple(
        _strip_category_prefix(f)
        for f in metadata_flags
        if not _strip_category_prefix(f).startswith(_EDITOR_FLAG_PREFIX)
        and not _strip_category_prefix(f).startswith(_FORENSIC_FLAG_PREFIXES)
    )


# Forensic-layer flag prefixes (2026-06-24). The three deterministic
# detectors in ``aegis.parser.forensic`` emit these literal prefixes:
#
#   * ``font_inconsistency_detected: <n> page(s); modal=<fam>``
#   * ``creator_mismatch_detected: detected=...; editing_tool=...; expected_one_of=[...]``
#   * ``text_overlay_detected: page(s) <list>; streams=<n>``
#
# ``font_inconsistency_detected`` and ``text_overlay_detected`` are
# emitted by ``parser.metadata.analyze_metadata`` and land on
# ``MetadataAnalysis.flags`` → ``DocumentRow.metadata_flags``.
# ``creator_mismatch_detected`` is appended by
# ``parser.pipeline.run_pipeline`` POST-extraction (it needs
# ``bank_name``) so it lands on ``DocumentRow.all_flags`` rather than
# ``metadata_flags``. Track A's ``DocumentIntegritySignals`` reads from
# both sources, so ``extract_forensic_signals`` scans
# ``validation_failures`` (which is populated from ``all_flags`` by
# ``dossier_panel._signals_for_document``) AND ``metadata_flags`` to
# pick up every flag regardless of which side of extraction emitted it.
_FORENSIC_FLAG_PREFIXES: Final[tuple[str, ...]] = (
    "font_inconsistency_detected",
    "creator_mismatch_detected",
    "text_overlay_detected",
)


# Per-signal rationale used when rendering the forensic finding as an
# EvidenceItem. Kept short so the operator-facing dossier row reads as
# the WHY, not as a parser dump. The literal flag detail (page list,
# editing-tool family, etc.) is preserved verbatim alongside.
_FORENSIC_RATIONALE: Final[dict[str, str]] = {
    "font_inconsistency_detected": (
        "Row-level font/size mismatch on transaction spans — paste-over fraud signature."
    ),
    "creator_mismatch_detected": (
        "PDF /Creator names an editing tool that doesn't match this bank's known-good profile."
    ),
    "text_overlay_detected": (
        "Multiple content streams render text at overlapping Y-ranges — paste-over signature."
    ),
}


# Short signal token surfaced on EvidenceItem.signal for dossier
# filtering. Matches the flag's leading identifier so the operator can
# filter "show me all text_overlay_detected" with the same UI primitive
# as "show me all reconciliation_failed_period".
_FORENSIC_SIGNAL_TOKEN: Final[dict[str, str]] = {
    "font_inconsistency_detected": "font_inconsistency_detected",
    "creator_mismatch_detected": "creator_mismatch_detected",
    "text_overlay_detected": "text_overlay_detected",
}


def extract_forensic_signals(
    metadata_flags: tuple[str, ...],
    validation_failures: tuple[str, ...],
) -> tuple[EvidenceItem, ...]:
    """Scan parser flags for the three forensic-layer signals and
    return them as EvidenceItems with plain-English rationale.

    Forensic signals (2026-06-24) are deterministic detectors that fire
    on specific paste-over fraud signatures:

    * ``font_inconsistency_detected`` — row-level font/size mismatch
    * ``creator_mismatch_detected`` — PDF /Creator doesn't match bank
    * ``text_overlay_detected`` — overlapping content-stream Y-ranges

    Each fires independently and contributes to ``metadata_score``
    inside the parser (weights +15 / +20 / +25 respectively, documented
    next to each detector's wiring). Track A surfaces them as
    EvidenceItems so the underwriter sees the specific forensic
    finding alongside the rest of the integrity evidence — same
    "show the WHAT" pattern as ``editor_detected`` and the
    reconciliation failures.

    De-duplicated across sources: a flag mirrored into both
    ``metadata_flags`` and ``validation_failures`` (via
    ``pipeline._collect_flags`` + ``dossier_panel._signals_for_document``)
    surfaces ONCE. Preserves the order: metadata source first, then
    validation_failures fallback; within each source, the order the
    parser emitted.
    """
    evidence: list[EvidenceItem] = []
    seen: set[str] = set()
    for source in (metadata_flags, validation_failures):
        for flag in source:
            stripped = _strip_category_prefix(flag)
            for prefix in _FORENSIC_FLAG_PREFIXES:
                if stripped.startswith(prefix) and prefix not in seen:
                    seen.add(prefix)
                    evidence.append(
                        EvidenceItem(
                            signal=_FORENSIC_SIGNAL_TOKEN[prefix],
                            detail=_format_forensic_detail(prefix, stripped),
                        ),
                    )
                    break
    return tuple(evidence)


def _format_forensic_detail(prefix: str, stripped_flag: str) -> str:
    """Build the EvidenceItem.detail string for one forensic flag.

    Combines the per-signal rationale ("WHY this matters to the
    underwriter") with the verbatim parser detail ("WHAT the detector
    found"). Truncates if necessary to fit
    ``EvidenceItem.detail`` ``max_length=240``.
    """
    rationale = _FORENSIC_RATIONALE[prefix]
    # ``stripped_flag`` looks like ``font_inconsistency_detected: 3
    # page(s); modal=Helvetica``. Strip the prefix + ": " so the detail
    # reads as the rationale + concrete finding, not a duplicated
    # signal token.
    after_prefix = stripped_flag[len(prefix) :].lstrip(": ").strip()
    if after_prefix:
        combined = f"{rationale} ({after_prefix})"
    else:
        combined = rationale
    # EvidenceItem.detail has max_length=240. Truncate with ellipsis
    # so a verbose parser flag doesn't crash Pydantic validation and
    # silently downgrade the verdict to None (mirrors the F1a
    # truncation pattern in framing.py).
    if len(combined) > 240:
        combined = combined[:237] + "..."
    return combined


__all__ = [
    "DRIFT_FAILURE_PREFIXES",
    "extract_drift_failures",
    "extract_editor_metadata_flag",
    "extract_forensic_signals",
    "extract_other_metadata_flags",
]
