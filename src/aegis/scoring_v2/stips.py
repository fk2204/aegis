"""Structured stipulations evaluator (Sprint 6 Track A).

Replaces the keyword-scan-only ``aegis.merchants.document_completeness``
with a bucketed view of a funder's ``conditional_requirements`` against
a merchant's document-on-file flags. Where the legacy checker only
surfaced "required-and-missing" warnings, this module returns four
buckets so the dossier can render red / green / yellow chips:

  * ``required`` — every non-empty requirement the funder published.
  * ``on_file`` — subset of required that the merchant has on file
    (green chip).
  * ``missing`` — subset of required that the merchant does NOT have
    on file (red chip). Includes structured kinds only; unknowns land
    in their own bucket.
  * ``unknown`` — subset that does not match any of the three keyword
    families (voided check / driver's license / N-month bank
    statements). Yellow chip — operator must visually confirm.

The keyword families + the per-family regex are intentionally kept in
parity with the legacy module so the migration is behaviour-preserving
for the three structured kinds: a bullet the legacy code flagged as
"missing voided check" lands here as ``missing`` with ``kind="voided_check"``;
a bullet the legacy code silently ignored ("personal guarantor signed")
lands here as ``unknown``. The legacy module's public ``check_completeness``
now delegates to this evaluator and filters back to the three structured
kinds so its callers see no behaviour change.

``is_hard`` distinguishes "must have" requirements from soft "preferred"
asks. The three structured kinds are always hard (the merchant either
has the document or doesn't — there's no fuzziness in the operator's
toggle). Unknowns are hard iff the bullet text contains "must",
"required", or "mandatory" (case-insensitive substring).

Pure module — no IO, no logging, no audit writes. Reusable from tests +
from the route layer.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=True,
    )


class StipItem(_StrictModel):
    """One stipulation surfaced to the operator.

    ``kind`` is a stable machine-readable string:
      * ``"voided_check"`` — bullet matches the voided-check regex.
      * ``"drivers_license"`` — bullet matches the driver's-license regex.
      * ``"bank_statements_months"`` — bullet matches the N-month regex.
      * ``"unknown"`` — bullet does not match any structured family.

    ``requirement_text`` is the funder bullet verbatim (stripped of
    leading / trailing whitespace) — the dossier displays it as-is so
    the operator sees what the funder actually said.

    ``on_file`` is True when the merchant has the documented item; for
    unknowns this is always False (the operator must confirm visually).

    ``missing_field`` names the ``MerchantRow`` attribute the operator
    flips on the merchant edit form to clear the red chip. None for
    unknowns (no automated path to clear them).

    ``is_hard`` is True for the three structured kinds (always — they
    are binary on/off documents) AND for unknowns whose text contains
    ``must`` / ``required`` / ``mandatory``. A hard-missing item gates
    the submit-to-funder button; a soft-missing item does not.
    """

    kind: str = Field(min_length=1)
    requirement_text: str = Field(min_length=1)
    on_file: bool
    missing_field: str | None = None
    is_hard: bool


class StipsResult(_StrictModel):
    """Bucketed view of a funder's conditional requirements vs a merchant.

    Invariants:
      * ``required`` is the union of ``on_file``, ``missing``, and
        ``unknown`` (an unknown bullet is always required — the
        operator must consider it).
      * ``on_file`` and ``missing`` are disjoint subsets of ``required``
        restricted to ``kind != "unknown"``.
      * ``unknown`` only contains items with ``kind == "unknown"``.
      * A given structured ``kind`` (voided_check / drivers_license /
        bank_statements_months) appears at most ONCE across ``required``
        — duplicate bullets that match the same family collapse.
      * Unknown bullets each get their own item — they're distinct
        requirements the operator must each evaluate.
    """

    required: list[StipItem] = Field(default_factory=list)
    on_file: list[StipItem] = Field(default_factory=list)
    missing: list[StipItem] = Field(default_factory=list)
    unknown: list[StipItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Scan keywords + regex (parity with merchants/document_completeness.py)
# ---------------------------------------------------------------------------


_VOIDED_CHECK_RE: Final[re.Pattern[str]] = re.compile(
    r"void(ed)?\s*check",
    re.IGNORECASE,
)

_DRIVERS_LICENSE_RE: Final[re.Pattern[str]] = re.compile(
    r"driver.{0,20}?license",
    re.IGNORECASE | re.DOTALL,
)

_N_MONTHS_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+)\s*(?:-|\s)?\s*(?:months?|mos?\b)",
    re.IGNORECASE,
)

_HARD_REQUIREMENT_TOKENS: Final[tuple[str, ...]] = ("must", "required", "mandatory")

# Sanity ceiling on N-months parsing. Real MCA funders never require
# more than 12 months; cap at 60 (5 years) so a stray "1000-month
# history" in funder prose doesn't permanently brick the submit gate.
_N_MONTHS_SANITY_CEILING: Final[int] = 60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_stips(funder: FunderRow, merchant: MerchantRow) -> StipsResult:
    """Bucket the funder's conditional requirements against the merchant.

    Empty ``funder.conditional_requirements`` returns an all-empty
    ``StipsResult`` trivially. Whitespace-only bullets are silently
    skipped (matches legacy behaviour).

    Duplicate bullets for the same structured family collapse to one
    item; unknown bullets each get their own item.
    """
    items: list[StipItem] = []
    seen_kinds: set[str] = set()

    for raw_requirement in funder.conditional_requirements:
        requirement = raw_requirement.strip()
        if not requirement:
            continue

        item = _classify_bullet(requirement, merchant)
        if item is None:
            continue

        if item.kind != "unknown":
            # Structured kinds collapse on duplicate.
            if item.kind in seen_kinds:
                continue
            seen_kinds.add(item.kind)

        items.append(item)

    on_file_items = [i for i in items if i.kind != "unknown" and i.on_file]
    missing_items = [i for i in items if i.kind != "unknown" and not i.on_file]
    unknown_items = [i for i in items if i.kind == "unknown"]

    return StipsResult(
        required=items,
        on_file=on_file_items,
        missing=missing_items,
        unknown=unknown_items,
    )


def _classify_bullet(requirement: str, merchant: MerchantRow) -> StipItem | None:
    """Map one funder bullet to a single ``StipItem``.

    Returns ``None`` only for an empty bullet (handled upstream). A
    bullet that matches more than one structured family is classified
    in priority order: voided_check → drivers_license →
    bank_statements_months → unknown. The priority order matches the
    legacy module's evaluation order so structured bullets that mention
    multiple families (rare in practice) get the same kind as before.
    """
    if _VOIDED_CHECK_RE.search(requirement):
        return StipItem(
            kind="voided_check",
            requirement_text=requirement,
            on_file=merchant.voided_check_on_file,
            missing_field="voided_check_on_file",
            is_hard=True,
        )

    if _DRIVERS_LICENSE_RE.search(requirement):
        return StipItem(
            kind="drivers_license",
            requirement_text=requirement,
            on_file=merchant.drivers_license_on_file,
            missing_field="drivers_license_on_file",
            is_hard=True,
        )

    n_months = _largest_n_months(requirement)
    if n_months is not None:
        return StipItem(
            kind="bank_statements_months",
            requirement_text=requirement,
            on_file=merchant.bank_statements_months >= n_months,
            missing_field="bank_statements_months",
            is_hard=True,
        )

    return StipItem(
        kind="unknown",
        requirement_text=requirement,
        on_file=False,
        missing_field=None,
        is_hard=_is_hard_text(requirement),
    )


def _is_hard_text(requirement: str) -> bool:
    """``True`` iff the bullet text contains ``must`` / ``required`` /
    ``mandatory`` (case-insensitive substring)."""
    lowered = requirement.lower()
    return any(token in lowered for token in _HARD_REQUIREMENT_TOKENS)


def _largest_n_months(requirement: str) -> int | None:
    """Pull the largest ``\\d+ month(s)`` capture from ``requirement``.

    Returns ``None`` when no N-month token is present. Caps the
    captured value at ``_N_MONTHS_SANITY_CEILING`` so a stray
    "1000-month history" pattern in funder prose doesn't permanently
    brick the submit-to-funder gate.
    """
    matches = _N_MONTHS_RE.findall(requirement)
    if not matches:
        return None
    try:
        ints = [int(m) for m in matches]
    except ValueError:
        return None
    largest = max(ints)
    if largest <= 0 or largest > _N_MONTHS_SANITY_CEILING:
        return None
    return largest


__all__ = [
    "StipItem",
    "StipsResult",
    "evaluate_stips",
]
