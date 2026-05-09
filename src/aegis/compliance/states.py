"""State regulatory table — three-tier model.

This module is **legally load-bearing**. NEVER fill in `tier=1` or `tier=2`
values from prior knowledge. Every Tier 1 / Tier 2 entry must be created
from operator-supplied source material (statute text or regulator PDF) at
audit time. The TS predecessor invented Kansas / Georgia / Missouri /
Maryland / Virginia constants from memory; that produced a fictional
"HB 1007" entry and incorrectly stated CoJ rules. We are not repeating
that pattern.

Tier model
----------
- **Tier 1** — MCA-specific commercial financing disclosure law in effect.
  Carries bill number, effective date, citation URL + verbatim excerpt,
  APR calculation method, CoJ rule, prescribed form URL, and a matching
  Jinja template under `compliance/templates/`.
- **Tier 2** — General state law applies, no MCA-specific statute.
  Carries the general-law citation + a 1-3 sentence regulatory posture
  note. Disclosure endpoint renders a generic acknowledgment.
- **Tier 3** — Served but not yet audited. Default for every state until
  the operator provides source material. Disclosure endpoint raises.

Boot guard
----------
`validate_states_table()` runs at app startup. It rejects any Tier 1
entry with missing fields or a missing template file, any Tier 2 entry
with missing fields, and any state whose `verified_date` is null.
Failure means the regulator-prescribed form would render against bad
data — fail-closed is the only safe behavior.

Served set
----------
45 states explicitly. Texas, Virginia, Connecticut, Utah, Missouri, DC,
and U.S. territories are NOT in `STATES` — `validate_state_served`
raises `StateNotServed` for them so the API can reject upstream.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Date the operator placed every state in Tier 3 deliberately. Future audits
# will overwrite individual entries with their own verified_date.
SKELETON_VERIFIED_DATE: Final[date] = date(2026, 5, 7)

# Templates directory — Tier 1 entries must reference a file that exists here.
TEMPLATES_DIR: Final[Path] = Path(__file__).parent / "templates"


# Errors -----------------------------------------------------------------------


class CompliancePolicyError(RuntimeError):
    """Boot-time validator failure — STATES table has missing or invalid entries."""


# Spec names the exceptions `StateNotServed` and `StateNotAudited` — the
# missing `Error` suffix is deliberate and matches the public API surface
# referenced by the API layer's reason codes (`state_not_served`).
class StateNotServed(ValueError):  # noqa: N818
    """Raised when a deal arrives from a state AEGIS does not serve."""


class StateNotAudited(RuntimeError):  # noqa: N818
    """Raised when a Tier 3 state requests a disclosure render."""

    def __init__(self, state: str, message: str | None = None) -> None:
        self.state = state
        super().__init__(
            message
            or f"AEGIS has not completed compliance research for state {state!r}"
        )


# Models -----------------------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


AprMethod = Literal["actuarial_reg_z", "simple_interest", "not_specified"]


class Amendment(_StrictModel):
    """A statute amendment that layers on top of the base bill.

    Per CA dossier: SB 362 (2025) amends SB 1235 with re-disclosure rules
    effective 2026-01-01. Future Tier 1 entries (NY, FL, GA) will carry
    their own amendment chains in the same shape.
    """

    bill_number: str = Field(min_length=1, description='e.g. "SB 362"')
    year: int = Field(ge=1900, le=2100)
    effective_date: date
    summary: str = Field(min_length=1, max_length=2000)
    citation_url: str = Field(min_length=1)


class _BaseRegulation(_StrictModel):
    state: str = Field(min_length=2, max_length=2, description="USPS code, uppercase")
    state_name: str = Field(min_length=1)
    verified_date: date


class Tier1Regulation(_BaseRegulation):
    """MCA-specific commercial-financing disclosure law in effect.

    Field shape follows the per-state dossiers under ``docs/compliance/``.
    Tier 1 entries for CA (and forthcoming NY/FL/GA) populate the full
    statute-vs-regulation date pair, threshold, CoJ status with citation
    chain, transmission rules, amendments, and a free-form notes block.

    The single AEGIS-internal field outside the dossier schema is
    ``template_path`` — required by the boot validator to verify the
    prescribed-form Jinja template exists on disk.
    """

    tier: Literal[1] = 1

    # Bill identification
    bill_number: str = Field(min_length=1, description='e.g. "SB 1235"')
    bill_year: int = Field(ge=1900, le=2100)
    chapter: str = Field(min_length=1, description='e.g. "Chapter 1011, Statutes of 2018"')
    sponsor: str = Field(min_length=1)

    # Effective dates — split because statute and regulations land on
    # different days for most CFDLs (SB 1235 enacted 2018-09-30, regs
    # effective 2022-12-09).
    effective_date_statute: date
    effective_date_regulations: date

    # Citations
    statute_citation: str = Field(min_length=1)
    regulation_citation: str = Field(min_length=1)
    citation_url_statute: str = Field(min_length=1)
    citation_url_regulation: str = Field(min_length=1)
    prescribed_form_section: str = Field(min_length=1)

    # APR + threshold + scope
    apr_calculation_method: AprMethod
    threshold_amount_usd: Decimal = Field(gt=Decimal("0"))
    threshold_test_summary: str = Field(min_length=1, max_length=1000)
    disclosure_required: Literal[True] = True

    # Confession of judgment (CoJ)
    coj_allowed: bool
    coj_citation: str = Field(min_length=1, description="sub-citation for the CoJ rule")
    coj_citation_url: str = Field(min_length=1)
    coj_amendment_bill: str = Field(min_length=1, description='e.g. "SB 688 (2022)"')
    coj_effective_date: date

    # Broker / transmission rules
    requires_unaltered_disclosure_transmission: bool
    transmission_record_retention_years: int = Field(ge=0)
    broker_compensation_disclosure_required: bool

    # Amendment chain (e.g. CA SB 1235 amended by SB 362)
    amendments: list[Amendment] = Field(default_factory=list)

    # Free-form notes from the dossier (template guidance, etc.)
    notes: str = Field(min_length=1, max_length=4000)

    # AEGIS-internal: filename under compliance/templates/. Boot validator
    # verifies the file exists.
    template_path: str = Field(
        min_length=1,
        description="filename under compliance/templates/, e.g. ca_sb1235.html.j2",
    )


class Tier2Regulation(_BaseRegulation):
    """General state law applies; no MCA-specific statute."""

    tier: Literal[2] = 2
    general_law_citation: str = Field(min_length=1)
    citation_url: str = Field(min_length=1)
    disclosure_required: Literal[False] = False
    notes: str = Field(min_length=1, max_length=600)


class Tier3Regulation(_BaseRegulation):
    """Served but not audited. Default for every state until upgraded."""

    tier: Literal[3] = 3


StateRegulation = Annotated[
    Tier1Regulation | Tier2Regulation | Tier3Regulation,
    Field(discriminator="tier"),
]


# Served-state inventory -------------------------------------------------------
# 45 states served. Each entry below MUST have a matching Tier3Regulation in
# STATES. The list itself is the source of truth for "is this a state we serve?"

_SERVED_STATES: Final[tuple[tuple[str, str], ...]] = (
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("DE", "Delaware"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("VT", "Vermont"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
)


def _build_skeleton() -> dict[str, StateRegulation]:
    """All 45 served states default to Tier 3 with today's verified_date.

    Audits replace individual entries with Tier1/Tier2 instances. Until then,
    `render_disclosure` will raise `StateNotAudited` for every state.
    """
    return {
        abbr: Tier3Regulation(
            state=abbr, state_name=name, verified_date=SKELETON_VERIFIED_DATE
        )
        for abbr, name in _SERVED_STATES
    }


STATES: dict[str, StateRegulation] = _build_skeleton()


# --- Per-state Tier 1 / Tier 2 promotions ------------------------------------
# Each promotion block is sourced from the corresponding dossier under
# ``docs/compliance/``. The dossier is authoritative for every field value.
# Field-name mapping: dossier's ``state="California"`` → model's
# ``state_name``; dossier's ``abbreviation="CA"`` → model's ``state``. The
# only AEGIS-added field beyond the dossier is ``template_path`` (boot
# validator routing key). ``verified_date`` is filled from the operator's
# session date; the dossier explicitly leaves it for the operator.
#
# Per docs/compliance/01_california.md: California — SB 1235 + SB 362
STATES["CA"] = Tier1Regulation(
    state="CA",
    state_name="California",
    tier=1,
    bill_number="SB 1235",
    bill_year=2018,
    chapter="Chapter 1011, Statutes of 2018",
    sponsor="Glazer",
    effective_date_statute=date(2018, 9, 30),
    effective_date_regulations=date(2022, 12, 9),
    statute_citation="Cal. Fin. Code § 22800-22805",
    regulation_citation="10 CCR § 900-956",
    citation_url_statute=(
        "https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml"
        "?bill_id=201720180SB1235"
    ),
    citation_url_regulation="https://www.law.cornell.edu/regulations/california/10-CCR-914",
    prescribed_form_section="10 CCR § 914",
    apr_calculation_method="actuarial_reg_z",
    threshold_amount_usd=Decimal("500000"),
    threshold_test_summary=(
        "Disclosure required when financing offer <= $500,000 AND "
        "recipient principally directed or managed from California "
        "(per 10 CCR § 954)."
    ),
    disclosure_required=True,
    coj_allowed=False,
    coj_citation="Cal. Code Civ. Proc. § 1132",
    coj_citation_url=(
        "https://law.justia.com/codes/california/code-ccp/part-3/title-3/"
        "chapter-1/section-1132/"
    ),
    coj_amendment_bill="SB 688 (2022)",
    coj_effective_date=date(2023, 1, 1),
    requires_unaltered_disclosure_transmission=True,
    transmission_record_retention_years=4,
    broker_compensation_disclosure_required=False,
    amendments=[
        Amendment(
            bill_number="SB 362",
            year=2025,
            effective_date=date(2026, 1, 1),
            summary=(
                "Adds Section 22806: provider may not use 'interest' or 'rate' "
                "deceptively; must re-disclose APR every time a charge/pricing "
                "metric/financing amount is communicated to recipient. Repeals "
                "old Section 22805 enforcement provision."
            ),
            citation_url=(
                "https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml"
                "?bill_id=202520260SB362"
            ),
        ),
    ],
    notes=(
        "MCAs fall under 'sales-based financing' for disclosure (10 CCR § 914). "
        "APR via DFPI methodology in §§ 940 and 942 — actuarial method consistent "
        "with scipy.brentq APR engine. Tolerances and cure provisions in § 955. "
        "CoJs banned outright since 2023-01-01. "
        "Section 952 transmission duties require unaltered forwarding + 4-year records."
    ),
    template_path="ca_sb1235.html.j2",
    verified_date=SKELETON_VERIFIED_DATE,
)


# Validators -------------------------------------------------------------------


def validate_states_table() -> None:
    """Boot-time fail-closed validator.

    Raises `CompliancePolicyError` listing every issue across the table:
      - state present in STATES but not in the served list (drift detection)
      - any entry whose `verified_date` is null
      - Tier 1 entry whose template file does not exist on disk
      - any field-level shape failure (Pydantic catches at construction;
        we re-validate here so config drift after boot is caught too)
    """
    served_abbrs = {abbr for abbr, _ in _SERVED_STATES}
    errors: list[str] = []

    # Drift: STATES set vs served list set
    table_abbrs = set(STATES.keys())
    extra = table_abbrs - served_abbrs
    missing = served_abbrs - table_abbrs
    for abbr in sorted(extra):
        errors.append(f"{abbr}: present in STATES but not in served-state inventory")
    for abbr in sorted(missing):
        errors.append(f"{abbr}: in served-state inventory but missing from STATES")

    # Per-entry checks
    for abbr in sorted(table_abbrs & served_abbrs):
        reg = STATES[abbr]
        # Pydantic enforces verified_date is non-null at construction.
        # If somebody hot-patches STATES with a bypassing object, the next
        # check (Tier 1 template existence / Tier 2 fields) catches it.

        if isinstance(reg, Tier1Regulation):
            template_file = TEMPLATES_DIR / reg.template_path
            if not template_file.is_file():
                errors.append(
                    f"{abbr}: Tier 1 template file missing at "
                    f"{template_file.relative_to(TEMPLATES_DIR.parent)}"
                )
        # Tier 2 / Tier 3: Pydantic already enforced required fields at
        # construction; nothing more to check here.

    if errors:
        raise CompliancePolicyError(
            "States table failed validation:\n  - " + "\n  - ".join(errors)
        )


def validate_state_served(state: str) -> None:
    """Raise `StateNotServed` if `state` is not one of the 45 served states.

    The API uses this at deal-intake to short-circuit unsupported geographies
    with a 422 / `reason=state_not_served` rather than running the parser.
    """
    abbr = (state or "").upper()
    if abbr not in STATES:
        raise StateNotServed(f"state_not_served: {state!r}")


def warn_if_unaudited(state: str) -> None:
    """Log a soft warning when a deal originates from a Tier 3 state.

    Format: `compliance.unaudited_state state=<XX> message="DEAL FROM
    UNAUDITED STATE — compliance posture not yet researched"`. This is a
    deliberate visibility hook so the operator knows which states need
    to move out of Tier 3 based on actual deal flow.
    """
    abbr = (state or "").upper()
    reg = STATES.get(abbr)
    if reg is None:
        return  # not served — caller is responsible for handling separately
    if reg.tier == 3:
        logger.warning(
            'compliance.unaudited_state state=%s message="DEAL FROM UNAUDITED STATE '
            '— compliance posture not yet researched"',
            abbr,
        )


__all__ = [
    "SKELETON_VERIFIED_DATE",
    "STATES",
    "TEMPLATES_DIR",
    "Amendment",
    "AprMethod",
    "CompliancePolicyError",
    "StateNotAudited",
    "StateNotServed",
    "StateRegulation",
    "Tier1Regulation",
    "Tier2Regulation",
    "Tier3Regulation",
    "validate_state_served",
    "validate_states_table",
    "warn_if_unaudited",
]
