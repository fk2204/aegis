"""Regex-based statement period detection — fallback layer.

When Bedrock returns null ``period_start`` / ``period_end`` (most often on
text-layer dropouts where the page-1 period block reads as layout chrome
and the model drops it), this module attempts to recover the dates from
the first-page text via a deterministic regex registry.

Routing contract
----------------
The fallback runs BEFORE the Pydantic ``StatementSummary`` validation
gate. When it matches, the recovered dates populate the missing fields
and the parse continues normally. When it fails, the original Pydantic
``ValidationError`` propagates and the document routes to
``manual_review`` with the existing ``period_unclear`` semantics. Net
effect: this layer can only LOOSEN a too-strict gate by recovering
documents Bedrock fumbled; it cannot route a clean document into
manual_review.

Per the AEGIS decision-boundary discipline (CLAUDE.md): loosening a
gate via a deterministic fallback that ONLY fires when Bedrock returned
null is a corpus-bounded change, not a tunable severity — the patterns
match real statement-text shape and never adjust based on an individual
deal.

Pattern registry
----------------
Six patterns, tried in order:

1. ``month_day_year_through`` — ``Month D, YYYY through Month D, YYYY``
2. ``slash_mdy_range``        — ``MM/DD/YYYY [-|to|through] MM/DD/YYYY``
3. ``slash_mdy2_range``       — ``MM/DD/YY [-|to|through] MM/DD/YY``
4. ``month_year_alone``       — ``Month YYYY`` (infers first..last day)
5. ``iso_range``              — ``YYYY-MM-DD [-|to|through] YYYY-MM-DD``
6. ``label_prefixed``         — ``Statement Period:|Period:|Statement Date:``
                                followed by any of patterns 1-5

Patterns 1-5 search the full text. Pattern 6 wraps them with a label
prefix and runs LAST so an unlabeled in-text match takes precedence
(some bank headers print the period twice — once under a label, once
inline — and the inline one is more reliable).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Final

# How much of the matched fragment to surface on the audit / flag tail.
# 200 chars is long enough to contain the matched dates + surrounding
# context for a one-line operator scan, short enough to keep the
# ``audit_log.details`` payload bounded.
_FRAGMENT_MAX_LEN: Final[int] = 200


@dataclass(frozen=True)
class PeriodMatch:
    """Result of a successful regex match.

    ``pattern_name`` is the registry key (e.g. ``"slash_mdy_range"``) so
    the audit row carries a stable, machine-greppable label. ``fragment``
    is the matched substring truncated to ``_FRAGMENT_MAX_LEN`` chars —
    safe for audit-log persistence (period text contains no PII per
    CLAUDE.md PII rules).
    """

    period_start: date
    period_end: date
    pattern_name: str
    fragment: str


_MONTH_NAMES: Final[dict[str, int]] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_MONTH_WORD = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|"
    r"Oct|Nov|Dec)"
)

# The en-dash (U+2013) / em-dash (U+2014) are intentional alternatives —
# bank statements regularly print period ranges with those separators.
_RANGE_SEP = r"(?:\s*(?:through|thru|to|-|–|—)\s*)"  # noqa: RUF001

# Pattern 1: ``January 1, 2026 through January 31, 2026``
_PATTERN_MONTH_DAY_YEAR_THROUGH = re.compile(
    rf"({_MONTH_WORD})\s+(\d{{1,2}}),?\s+(\d{{4}})"
    rf"{_RANGE_SEP}"
    rf"({_MONTH_WORD})\s+(\d{{1,2}}),?\s+(\d{{4}})",
    re.IGNORECASE,
)

# Pattern 2: ``01/01/2026 - 01/31/2026`` / ``01/01/2026 to 01/31/2026``
_PATTERN_SLASH_MDY_RANGE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})"
    rf"{_RANGE_SEP}"
    r"(\d{1,2})/(\d{1,2})/(\d{4})"
)

# Pattern 3: ``01/01/26 - 01/31/26`` (2-digit year, assume 20XX)
_PATTERN_SLASH_MDY2_RANGE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{2})\b"
    rf"{_RANGE_SEP}"
    r"(\d{1,2})/(\d{1,2})/(\d{2})\b"
)

# Pattern 4: ``January 2026`` standalone (infer first..last day of month).
# Anchored to a label prefix or line-start to avoid matching
# ``January 2026 statement summary follows`` as a period.
_PATTERN_MONTH_YEAR_ALONE = re.compile(
    rf"(?:^|\b)({_MONTH_WORD})\s+(\d{{4}})(?:\s+statement\b)?",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern 5: ``2026-01-01 - 2026-01-31`` ISO range
_PATTERN_ISO_RANGE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    rf"{_RANGE_SEP}"
    r"(\d{4})-(\d{2})-(\d{2})"
)

# Pattern 6: label-prefixed wrapper. Captures the prefix label so the
# fragment carries it through to the audit row.
_LABEL_PREFIX = re.compile(
    r"(?:Statement\s+Period|Statement\s+Date|Billing\s+Period|Period\s+Covered|Period)\s*[:\-]?\s*",
    re.IGNORECASE,
)


def _month_num(word: str) -> int | None:
    return _MONTH_NAMES.get(word.lower())


def _try_month_day_year_through(text: str) -> PeriodMatch | None:
    """Pattern 1 — ``Month D, YYYY through Month D, YYYY``."""
    m = _PATTERN_MONTH_DAY_YEAR_THROUGH.search(text)
    if m is None:
        return None
    sm = _month_num(m.group(1))
    em = _month_num(m.group(4))
    if sm is None or em is None:
        return None
    try:
        start = date(int(m.group(3)), sm, int(m.group(2)))
        end = date(int(m.group(6)), em, int(m.group(5)))
    except ValueError:
        return None
    return PeriodMatch(
        period_start=start,
        period_end=end,
        pattern_name="month_day_year_through",
        fragment=m.group(0)[:_FRAGMENT_MAX_LEN],
    )


def _try_slash_mdy_range(text: str) -> PeriodMatch | None:
    """Pattern 2 — ``MM/DD/YYYY [-|to|through] MM/DD/YYYY``."""
    m = _PATTERN_SLASH_MDY_RANGE.search(text)
    if m is None:
        return None
    try:
        start = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        end = date(int(m.group(6)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None
    return PeriodMatch(
        period_start=start,
        period_end=end,
        pattern_name="slash_mdy_range",
        fragment=m.group(0)[:_FRAGMENT_MAX_LEN],
    )


def _try_slash_mdy2_range(text: str) -> PeriodMatch | None:
    """Pattern 3 — ``MM/DD/YY [-|to|through] MM/DD/YY`` (assume 20XX)."""
    m = _PATTERN_SLASH_MDY2_RANGE.search(text)
    if m is None:
        return None
    try:
        # Assume 20XX — AEGIS only sees post-2000 statements in practice.
        # If a 19XX statement somehow reaches the parser, the period-window
        # validator (14-50 days) will catch the impossible diff.
        start = date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
        end = date(2000 + int(m.group(6)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None
    return PeriodMatch(
        period_start=start,
        period_end=end,
        pattern_name="slash_mdy2_range",
        fragment=m.group(0)[:_FRAGMENT_MAX_LEN],
    )


def _try_iso_range(text: str) -> PeriodMatch | None:
    """Pattern 5 — ``YYYY-MM-DD [-|to|through] YYYY-MM-DD``."""
    m = _PATTERN_ISO_RANGE.search(text)
    if m is None:
        return None
    try:
        start = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        end = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    return PeriodMatch(
        period_start=start,
        period_end=end,
        pattern_name="iso_range",
        fragment=m.group(0)[:_FRAGMENT_MAX_LEN],
    )


def _try_month_year_alone(text: str) -> PeriodMatch | None:
    """Pattern 4 — ``Month YYYY`` alone, infers first..last day of month."""
    m = _PATTERN_MONTH_YEAR_ALONE.search(text)
    if m is None:
        return None
    month = _month_num(m.group(1))
    if month is None:
        return None
    try:
        year = int(m.group(2))
        start = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        end = date(year, month, last_day)
    except ValueError:
        return None
    return PeriodMatch(
        period_start=start,
        period_end=end,
        pattern_name="month_year_alone",
        fragment=m.group(0)[:_FRAGMENT_MAX_LEN],
    )


def _try_label_prefixed(text: str) -> PeriodMatch | None:
    """Pattern 6 — ``Statement Period:`` (or similar) + any of 1-5.

    Runs the label-prefixed search by locating each label occurrence
    then attempting patterns 1-5 against a short window of text after
    the label. Wraps the matched fragment to include the label for
    audit visibility (operators eyeballing logs see ``Statement Period:
    01/01/2026 - 01/31/2026`` rather than the bare date range).
    """
    for label_match in _LABEL_PREFIX.finditer(text):
        # Look ahead up to 80 chars (a generous window covering any of
        # patterns 1-5's longest match). Bounded so a stray ``Period``
        # mention far from the dates doesn't pair them.
        window = text[label_match.end() : label_match.end() + 80]
        for sub_try in (
            _try_month_day_year_through,
            _try_slash_mdy_range,
            _try_slash_mdy2_range,
            _try_iso_range,
            _try_month_year_alone,
        ):
            sub = sub_try(window)
            if sub is None:
                continue
            label_text = label_match.group(0).rstrip()
            full_fragment = f"{label_text} {sub.fragment}"[:_FRAGMENT_MAX_LEN]
            return PeriodMatch(
                period_start=sub.period_start,
                period_end=sub.period_end,
                pattern_name=f"label_prefixed:{sub.pattern_name}",
                fragment=full_fragment,
            )
    return None


# Registry — order matters. Earlier entries win when multiple patterns
# could match the same text. Inline (label-less) matches take precedence
# over label-prefixed because in-text period strings are typically the
# bank's intended period of record; labeled fields are sometimes
# leftover boilerplate from a prior cycle.
_PATTERNS: Final[tuple[tuple[str, _TryFn], ...]] = (
    ("month_day_year_through", _try_month_day_year_through),
    ("slash_mdy_range", _try_slash_mdy_range),
    ("slash_mdy2_range", _try_slash_mdy2_range),
    ("iso_range", _try_iso_range),
    ("month_year_alone", _try_month_year_alone),
    ("label_prefixed", _try_label_prefixed),
)


# Defined here only so the registry annotation has a stable name. Each
# pattern function takes the raw text and returns a ``PeriodMatch`` or
# ``None``; the registry tuple captures (name, callable) pairs.
from collections.abc import Callable  # noqa: E402 — local to type alias

_TryFn = Callable[[str], "PeriodMatch | None"]


def extract_period_via_regex(text: str) -> PeriodMatch | None:
    """Return the first regex match across the registry, or ``None``.

    Empty / whitespace-only input short-circuits to ``None`` so callers
    don't pay regex compile/scan cost on degenerate inputs.

    The function is pure — no I/O, no logging. Callers (the extract
    layer) own the side effects (flag emission, audit row).
    """
    if not text or not text.strip():
        return None
    for _name, try_fn in _PATTERNS:
        match = try_fn(text)
        if match is not None:
            return match
    return None


__all__ = [
    "PeriodMatch",
    "extract_period_via_regex",
]
