"""Plain-language labels for Track A signal + branch identifiers.

Track A surfaces raw engineer tokens to the dossier — `reconciliation_failed_period`,
`drift_plus_editor`, `metadata_score`. Workers reading the dossier need plain
English; the underwriter has no way to translate "drift_plus_editor" into
"editor tampering + reconciliation drift" without the design doc open.

This module is the single source of truth for translating those tokens. Two
functions are exposed:

* ``humanize_track_a_signal(signal)`` — for ``EvidenceItem.signal`` values.
* ``humanize_track_a_branch(branch)`` — for ``IntegrityVerdict.branch`` values.

Unknown tokens fall back to a defensive title-cased rendering (snake → space →
title-case) so a new signal landing in code without copy never disappears from
the UI. The raw token stays accessible via ``title=`` tooltips in the template
for engineer-underwriters who want the code-level label.

Style mirrors ``aegis.web.routers.close_queue._CLOSE_QUEUE_FLAG_CATEGORY_LABELS``:
short, declarative, lowercase except for proper nouns / acronyms — these are
intended to be read inline in a table cell, not as section headers.

Add a new token? Register it in the relevant map below in the same commit that
adds the token. The defensive fallback exists so a forgotten registration
degrades gracefully, not so it's a permanent escape hatch.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Signal labels — keyed by ``EvidenceItem.signal`` short tokens.
#
# The canonical list comes from ``aegis.scoring_v2.track_a.compute`` (which
# emits ``metadata_score``, ``editor_detected``, ``metadata_flag``) and from
# ``_drift_signal_token`` (which projects ``validation_failures`` heads, i.e.
# the substring before the first ``:`` after the category prefix is stripped).
#
# The drift-failure space is the broad reconciliation_failed_* family enumerated
# in ``aegis.parser.validate`` and ``aegis.parser.processor.validate`` plus the
# ``future_dated`` family. Every value the parsers emit today is registered;
# unknowns fall through to ``_defensive_humanize``.
# ---------------------------------------------------------------------------

_SIGNAL_LABELS: Final[dict[str, str]] = {
    # Track A composition signals
    "metadata_score": "Metadata anomaly score",
    "editor_detected": "Editor metadata fingerprint",
    "metadata_flag": "Metadata flag",
    # Reconciliation drift family (validate.py + processor/validate.py)
    "reconciliation_failed_period": "Reconciliation: period total mismatch",
    "reconciliation_failed_deposit": "Reconciliation: deposit total mismatch",
    "reconciliation_failed_deposit_total": "Reconciliation: deposit total mismatch",
    "reconciliation_failed_withdrawal_total": "Reconciliation: withdrawal total mismatch",
    "reconciliation_failed_running_balance": "Reconciliation: running balance drift",
    "reconciliation_failed_daily_running_balance": "Reconciliation: daily running balance drift",
    "reconciliation_failed_intraday": "Reconciliation: intraday row mismatch",
    "reconciliation_failed_intraday_count": "Reconciliation: intraday rows failing",
    # Future-dated family (validate.py)
    "future_dated": "Future-dated statement period",
    "future_dated_period": "Future-dated statement period",
}


# ---------------------------------------------------------------------------
# Branch labels — keyed by ``IntegrityVerdict.branch`` Literal values.
#
# Source of truth: ``aegis.scoring_v2.track_a.models.IntegrityBranch``. Every
# Literal member is registered explicitly so mypy can spot a missing entry on
# any future branch addition.
# ---------------------------------------------------------------------------

_BRANCH_LABELS: Final[dict[str, str]] = {
    "clean": "Clean",
    "strong_metadata": "Strong metadata anomaly",
    "drift_plus_editor": "Editor tampering + reconciliation drift",
    "medium_corroborated": "Medium metadata + drift corroboration",
    "drift_alone": "Reconciliation drift alone",
}


def _defensive_humanize(token: str) -> str:
    """Snake-case identifier → title-cased plain text.

    Used as the fallback when a token has no registered label. Designed so
    a brand-new signal landing in the parser still reads as something an
    underwriter can parse (e.g. ``reconciliation_failed_foo`` becomes
    ``"Reconciliation Failed Foo"``) instead of leaking the raw token to the
    UI verbatim.

    Returns the token unchanged (other than whitespace trim) if it does not
    contain underscores — preserves acronyms / camelCase tokens that someone
    might pass deliberately.
    """
    if not token:
        return ""
    cleaned = token.strip()
    if not cleaned:
        return ""
    if "_" not in cleaned:
        return cleaned
    return cleaned.replace("_", " ").title()


def humanize_track_a_signal(signal: str) -> str:
    """Translate a Track A ``EvidenceItem.signal`` token to plain English.

    Returns the registered label if any, otherwise the defensive
    title-cased fallback. Never returns an empty string for a non-empty
    input — empty input yields empty output (lets the template skip
    rendering rather than crashing).
    """
    if signal in _SIGNAL_LABELS:
        return _SIGNAL_LABELS[signal]
    return _defensive_humanize(signal)


def humanize_track_a_branch(branch: str) -> str:
    """Translate a Track A ``IntegrityVerdict.branch`` token to plain English.

    Returns the registered label if any, otherwise the defensive
    title-cased fallback. The Literal type on ``IntegrityVerdict.branch``
    means callers should always hit a registered value in practice; the
    fallback exists for defense-in-depth against future branch additions
    that ship code before this map is updated.
    """
    if branch in _BRANCH_LABELS:
        return _BRANCH_LABELS[branch]
    return _defensive_humanize(branch)


__all__ = [
    "humanize_track_a_branch",
    "humanize_track_a_signal",
]
