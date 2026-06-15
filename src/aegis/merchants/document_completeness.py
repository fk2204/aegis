"""Document-completeness checker — Feature 2 (2026-06-15 operator directive).

The "Submit to Funder" button on the dossier needs a pre-flight check
against the top-matched funder's ``conditional_requirements``: if the
funder requires a voided check, a copy of the owner's driver's license,
or N months of bank statements, those documents must be on file before
the operator can submit. This module owns the scan + comparison
against the per-merchant on-file flags from migration 061.

The funder's ``conditional_requirements`` is a tuple of free-text
strings (see ``aegis.funders.models.FunderRow``). This module does a
case-insensitive keyword scan per known requirement type:

  * "voided check"                  -> merchant.voided_check_on_file
  * "driver" + "license"            -> merchant.drivers_license_on_file
  * "<N> months"  (regex)           -> merchant.bank_statements_months >= N
  * "last <N> month" / "<N>-month"  -> same as above

Anything else is left alone — the operator owns confirmation of
non-checker-tracked conditions through the existing dossier review
flow. The point of the checker is to catch the easy misses, not to
encode every possible funder stipulation.

The returned warnings are surfaced to the operator on the dossier
banner; the submit-to-funder route refuses (400) when the top matched
funder has any unmet requirement.

Pure module — no IO, no logging, no audit writes. Reusable from tests
+ from the route layer.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow


class DocumentCompletenessWarning(BaseModel):
    """One missing-document warning surfaced to the operator.

    ``requirement_kind`` is a stable machine-readable string the
    template can use to render a per-warning chip. ``requirement_text``
    is the exact funder requirement string the scan matched against
    (verbatim so the operator can see what the funder said). ``missing_field``
    is the ``MerchantRow`` attribute the operator should toggle on the
    intake / merchant-edit form to clear the warning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_kind: str = Field(min_length=1)
    requirement_text: str = Field(min_length=1)
    missing_field: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Scan keywords + regex
# ---------------------------------------------------------------------------


# Substring (case-insensitive) signalling "voided check" requirement.
_VOIDED_CHECK_RE: Final[re.Pattern[str]] = re.compile(
    r"voided\s*check",
    re.IGNORECASE,
)

# "driver" + "license" together. Accepts "driver license", "drivers
# license", "driver's license", "Driver's License", "DL (drivers
# license)" by spanning up to ~20 arbitrary characters between the two
# tokens. ``re.DOTALL`` lets the gap span apostrophes and spaces
# uniformly.
_DRIVERS_LICENSE_RE: Final[re.Pattern[str]] = re.compile(
    r"driver.{0,20}?license",
    re.IGNORECASE | re.DOTALL,
)

# Numeric N-months requirement. Matches "6 months", "6-month",
# "last 6 months", "6 mos", "6 mo", etc. Captures the digit count.
# Word-form numbers (e.g. "six") are intentionally out of scope —
# operators tend to use digits and the regex stays simple + fast.
_N_MONTHS_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+)\s*(?:-|\s)?\s*(?:months?|mos?\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_completeness(
    *,
    merchant: MerchantRow,
    funder: FunderRow,
) -> list[DocumentCompletenessWarning]:
    """Compare ``funder.conditional_requirements`` against the merchant's
    document-on-file flags. Return one warning per unmet requirement.

    Empty list is the "all clear" signal — the submit-to-funder gate
    only opens when this returns ``[]``. An empty
    ``conditional_requirements`` tuple trivially returns ``[]``.
    """
    warnings: list[DocumentCompletenessWarning] = []
    seen_tokens: set[str] = set()

    for raw_requirement in funder.conditional_requirements:
        requirement = raw_requirement.strip()
        if not requirement:
            continue

        # Voided check: one warning per funder, regardless of how many
        # strings mention it.
        if (
            _VOIDED_CHECK_RE.search(requirement)
            and not merchant.voided_check_on_file
            and "voided_check" not in seen_tokens
        ):
            warnings.append(
                DocumentCompletenessWarning(
                    requirement_kind="voided_check",
                    requirement_text=requirement,
                    missing_field="voided_check_on_file",
                )
            )
            seen_tokens.add("voided_check")

        # Driver's license: same single-token logic.
        if (
            _DRIVERS_LICENSE_RE.search(requirement)
            and not merchant.drivers_license_on_file
            and "drivers_license" not in seen_tokens
        ):
            warnings.append(
                DocumentCompletenessWarning(
                    requirement_kind="drivers_license",
                    requirement_text=requirement,
                    missing_field="drivers_license_on_file",
                )
            )
            seen_tokens.add("drivers_license")

        # N-month bank statements: take the largest N if multiple are
        # mentioned in the same requirement string — the funder's
        # strictest mention wins.
        n_months_match = _largest_n_months(requirement)
        if (
            n_months_match is not None
            and merchant.bank_statements_months < n_months_match
            and "bank_statements_months" not in seen_tokens
        ):
            warnings.append(
                DocumentCompletenessWarning(
                    requirement_kind="bank_statements_months",
                    requirement_text=requirement,
                    missing_field="bank_statements_months",
                )
            )
            seen_tokens.add("bank_statements_months")

    return warnings


def _largest_n_months(requirement: str) -> int | None:
    """Pull the largest ``\\d+ month(s)`` capture from ``requirement``.

    Returns ``None`` when no N-month token is present. Caps the
    captured value at a sanity ceiling (60 months = 5 years) so a
    stray "1000-month history" pattern in funder prose doesn't
    permanently brick the submit-to-funder gate. Real funders never
    require more than the last 12 months on the MCA path.
    """
    matches = _N_MONTHS_RE.findall(requirement)
    if not matches:
        return None
    try:
        ints = [int(m) for m in matches]
    except ValueError:
        return None
    largest = max(ints)
    if largest <= 0 or largest > 60:
        return None
    return largest


__all__ = [
    "DocumentCompletenessWarning",
    "check_completeness",
]
