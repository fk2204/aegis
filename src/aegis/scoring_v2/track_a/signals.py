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
        f for f in validation_failures
        if _strip_category_prefix(f).startswith(DRIFT_FAILURE_PREFIXES)
    )


def extract_editor_metadata_flag(
    metadata_flags: tuple[str, ...],
) -> str | None:
    """Return the literal editor flag string if any, otherwise None.

    Returns only the FIRST one found — multiple ``editor_detected``
    flags on the same document are uncommon enough to not be a real
    signal differentiator. The presence is what matters.

    The returned string is normalised to the unprefixed parser form
    so downstream evidence renders consistently regardless of
    whether the caller passed flags from the in-memory ValidationResult
    or the persisted DB column.
    """
    for f in metadata_flags:
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
    does."""
    return tuple(
        _strip_category_prefix(f) for f in metadata_flags
        if not _strip_category_prefix(f).startswith(_EDITOR_FLAG_PREFIX)
    )


__all__ = [
    "DRIFT_FAILURE_PREFIXES",
    "extract_drift_failures",
    "extract_editor_metadata_flag",
    "extract_other_metadata_flags",
]
