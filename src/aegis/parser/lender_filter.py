"""Deterministic second-pass filter for lender / capital-platform deposits.

R0.2 from the 2026-06-08 audit. The LLM classifier (pass 2) currently
labels MCA-funder ACH credits, SBA loan proceeds, line-of-credit draws,
and capital-platform advances as `ach_credit` / `deposit` / `transfer`
when the descriptor doesn't make the source obvious. The aggregator only
nets out LLM-tagged `transfer` rows from true revenue, so misclassified
lender proceeds inflate revenue and (downstream) the suggested max
advance.

This module supplies a deterministic substring filter the aggregator
runs over every positive-amount row in `_REVENUE_INCLUDED` so a known
lender name knocks the row out of revenue regardless of LLM label. It is
intentionally conservative — substring + cleaned-string only, no fuzzy
match. Fuzzy match (Levenshtein / SequenceMatcher) is R1.1 and lives in
`parser/patterns.py`, where the fraud detectors already need it.

Key design choices
------------------
- Names that would alias real payment processors are required to be
  prefix-paired with a lending qualifier. E.g. "SQUARE" alone is
  Square's settlement processor (real revenue); "SQUARE CAPITAL" is the
  lending product. We match only the qualified form to avoid nuking
  legitimate processor settlements.
- Names are normalized to uppercase with punctuation stripped and runs
  of whitespace collapsed. Substring match runs against this normalized
  form, so "OnDeck-Funding, ADV" and "ONDECK FUNDING ADV" both match
  "ONDECK".
- The function returns the matched name so the aggregator can record
  WHY a row was excluded; the underwriter then sees "ONDECK $25,000
  excluded as lender proceed" in the dossier flag stream rather than a
  silent revenue cut.
"""

from __future__ import annotations

import re
from typing import Final

# Substrings that, when found inside a normalized (upper-case,
# punctuation-stripped) deposit description, identify the row as
# lender / capital-platform proceeds rather than business revenue.
#
# Rules of inclusion:
#   - MCA funders & dedicated SMB lenders ship under their funder name.
#   - Capital-platform brands MUST include the qualifier ("CAPITAL",
#     "LENDING", "WORKING CAPITAL"). Bare brands ("SQUARE", "STRIPE",
#     "PAYPAL", "SHOPIFY", "AMAZON") are processor settlements and
#     real revenue.
#   - SBA programs are unambiguous loan proceeds.
#   - Generic lending phrases ("LINE OF CREDIT", "MERCHANT LOAN")
#     describe the product directly and are safe substrings.
#
# Conservative substring match — false positives nuke real revenue and
# the operator has explicitly said the bigger risk is silently inflated
# revenue, not under-recognized revenue. When in doubt the row stays in.
KNOWN_LENDERS: Final[frozenset[str]] = frozenset(
    {
        # ---- MCA funders ----
        "ONDECK",
        "KABBAGE",
        "FUNDBOX",
        "BLUEVINE",
        "FORWARD FINANCING",
        "RAPID FINANCE",
        "CREDIBLY",
        "KAPITUS",
        "MULLIGAN",
        "CFG",
        "PEARL CAPITAL",
        "EVEREST",
        "YELLOWSTONE",
        "VELOCITY",
        "FOX BUSINESS",
        "WG FUND",
        "SETTLEMENT ADVANCE",
        # ---- Capital platforms (qualifier required so processor
        #      settlement deposits don't match) ----
        "SQUARE CAPITAL",
        "SHOPIFY CAPITAL",
        "STRIPE CAPITAL",
        "PAYPAL WORKING CAPITAL",
        "AMAZON LENDING",
        "INTUIT CAPITAL",
        # ---- Banks / SBA / consumer-style lenders also funding SMB ----
        "SBA EIDL",
        "SBA PPP",
        "FUNDING CIRCLE",
        "LENDIO",
        "LIVE OAK",
        "CELTIC",
        "WEBBANK",
        # ---- Generic lending product terms ----
        "BUSINESS LOAN",
        "MERCHANT LOAN",
        "MERCHANT ADVANCE",
        "REVENUE BASED FINANCING",
        "REVENUE BASED LENDING",
        "DAILY ADVANCE",
        "CAPITAL LOAN",
        "LINE OF CREDIT",
        "LOC ADVANCE",
        "LOC DRAW",
    }
)


# Anything that is NOT a letter, digit, or whitespace becomes a space
# during normalization. Keeps multi-word phrases like "REVENUE BASED
# FINANCING" intact while collapsing "ONDECK-FUNDING,INC" into a single
# token boundary so "ONDECK" still matches as a substring.
_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9\s]")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _normalize(description: str) -> str:
    """Upper-case, punctuation-stripped, whitespace-collapsed form.

    Same transform applied to both the description and (implicitly via
    the curated upper-case set) the lender keys. Idempotent: passing an
    already-normalized string returns it unchanged.
    """
    if not description:
        return ""
    no_punct = _PUNCT_RE.sub(" ", description)
    collapsed = _WHITESPACE_RE.sub(" ", no_punct).strip()
    return collapsed.upper()


def is_lender_proceed(description: str) -> tuple[bool, str | None]:
    """Return ``(True, matched_name)`` if the description looks like
    lender / capital-platform proceeds.

    Substring match runs against the normalized description. The first
    matching lender name (iteration order is set-defined and stable per
    Python build, but callers should not rely on which name wins when
    multiple are present — they should treat the match as a tag, not as
    canonical attribution).

    Returns ``(False, None)`` when no key matches.
    """
    normalized = _normalize(description)
    if not normalized:
        return False, None
    # Sort longest-first so "REVENUE BASED LENDING" wins over a shorter
    # accidental overlap with a future generic key. Stable per call.
    for key in sorted(KNOWN_LENDERS, key=len, reverse=True):
        if key in normalized:
            return True, key
    return False, None


__all__ = ["KNOWN_LENDERS", "is_lender_proceed"]
