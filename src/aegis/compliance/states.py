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


AprMethod = Literal[
    "actuarial_reg_z",
    "simple_interest",
    "not_specified",
    # FL FCFDL: § 559.9613 lists 6 required content items; APR is NOT
    # among them. ``"not_required"`` is a meaningfully different signal
    # from ``"not_specified"`` — it documents that the regulator
    # deliberately omitted APR rather than that AEGIS hasn't researched
    # the method.
    "not_required",
]
CojStatus = Literal["allowed", "banned", "conditional"]


class Amendment(_StrictModel):
    """An ENACTED statute amendment that layers on top of the base bill.

    Per CA dossier: SB 362 (2025) amends SB 1235 with re-disclosure rules
    effective 2026-01-01. Future Tier 1 entries with enacted amendments
    use this shape. Pending / introduced bills go in `PendingAmendment`.
    """

    bill_number: str = Field(min_length=1, description='e.g. "SB 362"')
    year: int = Field(ge=1900, le=2100)
    effective_date: date
    summary: str = Field(min_length=1, max_length=2000)
    citation_url: str = Field(min_length=1)


class PendingAmendment(_StrictModel):
    """A bill that is INTRODUCED / PENDING — not yet enacted.

    Per NY dossier: S2305 (2025) would prohibit CoJs on debts < $5M but
    is still pending. Tracked here so the operator dashboard can flag
    affected states and so the matcher can warn before the bill changes
    status.
    """

    bill_number: str = Field(min_length=1, description='e.g. "S2305"')
    year: int = Field(ge=1900, le=2100)
    status: Literal["introduced", "pending", "in_committee"] = "introduced"
    summary: str = Field(min_length=1, max_length=2000)
    citation_url: str = Field(min_length=1)


class _BaseRegulation(_StrictModel):
    state: str = Field(min_length=2, max_length=2, description="USPS code, uppercase")
    state_name: str = Field(min_length=1)
    verified_date: date


class Tier1Regulation(_BaseRegulation):
    """MCA-specific commercial-financing disclosure law in effect.

    Field shape follows the per-state dossiers under ``docs/compliance/``.
    Many fields are OPTIONAL because dossiers vary in coverage — e.g. the
    CA dossier provides bill_year + chapter + sponsor + statute enactment
    date; the NY dossier glues the bill+amendment year into a single
    ``bill_number`` string and does not quote SB 5470's enactment date.
    Each dossier populates the fields it speaks to; the rest stay null.

    The single AEGIS-internal field outside any dossier schema is
    ``template_path`` — required by the boot validator to verify the
    prescribed-form Jinja template exists on disk.
    """

    tier: Literal[1] = 1

    # Bill identification — bill_number is required; richer attribution
    # is optional because not every dossier supplies it.
    bill_number: str = Field(min_length=1, description='e.g. "SB 1235"')
    bill_year: int | None = Field(default=None, ge=1900, le=2100)
    chapter: str | None = Field(default=None, min_length=1)
    sponsor: str | None = Field(default=None, min_length=1)
    common_name: str | None = Field(
        default=None,
        min_length=1,
        description='e.g. "Commercial Finance Disclosure Law (CFDL)"',
    )

    # Effective dates — both optional because dossiers may quote one or
    # the other (or neither, in pending/audited-but-not-yet-enacted cases).
    # ``effective_date_regulations`` is the binding compliance date.
    effective_date_statute: date | None = None
    effective_date_regulations: date | None = None

    # NY-specific date pair: regulations were ADOPTED 2023-02-01 but
    # mandatory compliance only kicked in 2023-08-01. Other states use
    # the single ``effective_date_regulations`` field above.
    regulations_adopted: date | None = None
    mandatory_compliance_date: date | None = None

    # Citations. Several are optional because not every Tier 1 state has
    # a separate body of implementing regulations: FL's FCFDL is statute-
    # self-executing (§ 559.961-559.9615 has no companion CCR-style regs),
    # and some states are content-based rather than form-prescribed.
    statute_citation: str = Field(min_length=1)
    regulation_citation: str | None = Field(default=None, min_length=1)
    citation_url_statute: str = Field(min_length=1)
    citation_url_regulation: str | None = Field(default=None, min_length=1)
    # Optional: FL is content-based per § 559.9613 (no prescribed form).
    prescribed_form_section: str | None = Field(default=None, min_length=1)

    # APR + threshold + scope
    apr_calculation_method: AprMethod
    # FL: no APR is required at all. Default True (CA/NY require APR);
    # FL flips this False so disclosure renderers and pricing guards can
    # check one field rather than re-deriving from apr_calculation_method.
    apr_required: bool = True
    apr_tolerance_percent: Decimal | None = Field(
        default=None,
        description="e.g. NY § 600.4: 0.125 (regular) or 0.250 (irregular)",
    )
    apr_tolerance_irregular_percent: Decimal | None = None
    apr_re_disclosure_required: bool = False
    threshold_amount_usd: Decimal = Field(gt=Decimal("0"))
    threshold_test_summary: str | None = Field(default=None, min_length=1, max_length=1000)
    disclosure_required: Literal[True] = True

    # Confession of judgment (CoJ). Tristate per the dossiers:
    #   "allowed"     — CoJ permitted, no state-level restriction
    #   "banned"      — CoJ unenforceable in this state (CA, FL)
    #   "conditional" — permitted with restrictions (NY: residents only)
    coj_allowed: CojStatus
    coj_citation: str = Field(min_length=1, description="sub-citation for the CoJ rule")
    coj_citation_url: str = Field(min_length=1)
    coj_amendment_bill: str | None = Field(default=None, min_length=1)
    # Optional: FL § 55.05 is a historic statute (predates MCAs by ~century);
    # the dossier does not quote a specific effective date.
    coj_effective_date: date | None = None

    # Broker / transmission rules
    requires_unaltered_disclosure_transmission: bool
    transmission_record_retention_years: int = Field(ge=0)
    broker_compensation_disclosure_required: bool
    broker_disclosure_section: str | None = Field(
        default=None,
        description='e.g. "23 NYCRR § 600.21(f)"',
    )
    # FL FCFDL § 559.9614(1)(a): brokers may not assess, collect, or
    # solicit advance fees from merchants. Default False (most states are
    # silent); FL flips True. AEGIS uses this signal to hard-fail any
    # match where a funder's funder.charges_merchant_advance_fees=True
    # is paired with a merchant in this state.
    broker_advance_fees_prohibited: bool = False
    # FL FCFDL § 559.9614(3): a broker advertising services must disclose
    # actual address and phone of the broker's business and any forwarding
    # service used. Operational rule (marketing copy review), not a
    # runtime computation; flag here so the dashboard can surface it.
    broker_advertisement_address_disclosure_required: bool = False

    # Enforcement posture (FL: AG-only, no PRA; CA: DFPI; NY: DFS).
    # ``private_right_of_action`` defaults True because most state UDAP
    # frameworks include a PRA; states that exclude one (FL FCFDL
    # § 559.9615) flip it False. ``enforcement_authority`` is a free-form
    # short label for the dashboard.
    private_right_of_action: bool = True
    enforcement_authority: str | None = Field(default=None, min_length=1)

    # Amendment chains
    amendments: list[Amendment] = Field(default_factory=list)
    pending_amendments: list[PendingAmendment] = Field(default_factory=list)

    # Free-form notes from the dossier (template guidance, etc.)
    notes: str = Field(min_length=1, max_length=4000)

    # AEGIS-internal: filename under compliance/templates/. Boot validator
    # verifies the file exists. Even content-based states (FL) get a
    # template — the rendered HTML is just a written disclosure rather
    # than the regulator's prescribed table.
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
    coj_allowed="banned",
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


# Per docs/compliance/02_new_york.md: New York — CFDL Article 8 + 23 NYCRR
# Part 600. Field-name mapping follows the CA convention: dossier's
# ``state="New York"`` → ``state_name``; ``abbreviation="NY"`` → ``state``.
# The dossier glues SB 5470 + S898 into ``bill_number`` rather than
# splitting them into a base+amendment pair, and does not quote SB 5470's
# enactment date — both are preserved as the dossier provides them
# (``effective_date_statute`` left None; bill_year is the year of the
# original SB 5470 enactment per ``"SB 5470 (2020)"``).
STATES["NY"] = Tier1Regulation(
    state="NY",
    state_name="New York",
    tier=1,
    bill_number="SB 5470 (2020), amended by S898 (2021)",
    bill_year=2020,
    common_name="Commercial Finance Disclosure Law (CFDL)",
    # statute enactment date not specified in the dossier — leave null
    # rather than improvise.
    effective_date_statute=None,
    # NY's mandatory compliance date doubles as the binding regulations-
    # effective date for the matcher / boot validator.
    effective_date_regulations=date(2023, 8, 1),
    regulations_adopted=date(2023, 2, 1),
    mandatory_compliance_date=date(2023, 8, 1),
    statute_citation="N.Y. Fin. Services Law §§ 801-811",
    regulation_citation="23 NYCRR Part 600",
    citation_url_statute=(
        "https://www.dfs.ny.gov/industry_guidance/regulations/"
        "final_financial_services/rf_finservices_23nycrr600_text"
    ),
    citation_url_regulation="https://www.law.cornell.edu/regulations/new-york/23-NYCRR-600.6",
    prescribed_form_section="23 NYCRR § 600.6",
    apr_calculation_method="actuarial_reg_z",
    # Per CORRECTIONS_2026-05-08.md: the tolerance section is § 600.4,
    # NOT § 600.5 (which covers signatures). 0.125% regular, 0.25% irregular.
    apr_tolerance_percent=Decimal("0.125"),
    apr_tolerance_irregular_percent=Decimal("0.250"),
    apr_re_disclosure_required=True,
    threshold_amount_usd=Decimal("2500000"),
    threshold_test_summary=(
        "Disclosure required when financing offer <= $2,500,000 AND a specific "
        "offer is extended to the recipient (per 23 NYCRR § 600.0)."
    ),
    disclosure_required=True,
    coj_allowed="conditional",
    coj_citation="N.Y. CPLR § 3218 (amended chapter 311 of 2019)",
    coj_citation_url="https://law.justia.com/codes/new-york/cvp/article-32/3218/",
    coj_amendment_bill="Chapter 311 of 2019",
    coj_effective_date=date(2019, 8, 30),
    requires_unaltered_disclosure_transmission=True,
    transmission_record_retention_years=4,
    broker_compensation_disclosure_required=True,
    broker_disclosure_section="23 NYCRR § 600.21(f)",
    pending_amendments=[
        PendingAmendment(
            bill_number="S2305",
            year=2025,
            status="introduced",
            summary=(
                "Would prohibit CoJs on debts < $5,000,000 and on consumer / "
                "non-business debts. If enacted, AEGIS should default "
                "coj_allowed='banned' for NY MCA deals < $5M."
            ),
            citation_url="https://www.nysenate.gov/legislation/bills/2025/S2305",
        ),
    ],
    notes=(
        "MCAs are 'sales-based financing' under § 803 / 23 NYCRR § 600.6. "
        "10-row disclosure (one more than CA — adds Collateral Requirements). "
        "Includes anti-double-dipping disclosure for renewal financing per "
        "§ 600.6(b)(3)(v). APR re-disclosure required at every pricing "
        "communication (built into the regulations from inception, not a "
        "later amendment as in CA). CoJs allowed against NY-resident "
        "merchants only since 2019-08-30. Pending bill S2305 would ban CoJs "
        "on debts < $5M — track for status. Broker compensation disclosure "
        "REQUIRED — separate written notice from provider per § 600.21(f). "
        "60-day bona fide error cure under § 600.22."
    ),
    template_path="ny_cfdl.html.j2",
    verified_date=SKELETON_VERIFIED_DATE,
)


# Per docs/compliance/03_florida.md: Florida — FCFDL HB 1353 (2023),
# Fla. Stat. §§ 559.961-559.9615 (Part XIII of Chapter 559). Field-name
# mapping follows CA/NY convention: dossier's ``state="Florida"`` →
# ``state_name``; ``abbreviation="FL"`` → ``state``.
#
# FL differs from CA/NY in three structural ways the model now reflects:
#   1. No separate body of regulations — statute self-executing. Hence
#      ``regulation_citation`` and ``citation_url_regulation`` are None.
#   2. Content-based, not form-prescribed (§ 559.9613 lists six required
#      content items; no row/column table mandated). Hence
#      ``prescribed_form_section`` is None.
#   3. No APR required (lighter than CA / NY / GA). Hence
#      ``apr_calculation_method="not_required"`` and ``apr_required=False``.
#
# Broker-side fields ``broker_advance_fees_prohibited`` (§ 559.9614(1)(a))
# and ``broker_advertisement_address_disclosure_required`` (§ 559.9614(3))
# are FL-specific — neither CA nor NY uses them.
STATES["FL"] = Tier1Regulation(
    state="FL",
    state_name="Florida",
    tier=1,
    bill_number="HB 1353",
    bill_year=2023,
    chapter="Chapter 2023-290, Laws of Florida",
    sponsor="DeSantis (signed)",
    common_name="Florida Commercial Financing Disclosure Law (FCFDL)",
    effective_date_statute=date(2023, 7, 1),
    # FL: mandatory compliance on/after 2024-01-01 doubles as the
    # effective regulations date for the matcher.
    effective_date_regulations=date(2024, 1, 1),
    mandatory_compliance_date=date(2024, 1, 1),
    statute_citation="Fla. Stat. §§ 559.961 - 559.9615 (Part XIII of Chapter 559)",
    # FL has no separate implementing regulations — statute is
    # self-executing. Both regulation_citation and the URL are null.
    regulation_citation=None,
    citation_url_statute="https://www.flsenate.gov/Laws/Statutes/2024/0559.9613",
    citation_url_regulation=None,
    # Content-based, not form-prescribed.
    prescribed_form_section=None,
    apr_calculation_method="not_required",
    apr_required=False,
    threshold_amount_usd=Decimal("500000"),
    threshold_test_summary=(
        "Disclosure required when financing offer <= $500,000 AND "
        "business located in Florida AND provider consummates more than "
        "5 commercial financing transactions in Florida per calendar "
        "year (per Fla. Stat. § 559.9612 + § 559.9611)."
    ),
    disclosure_required=True,
    coj_allowed="banned",
    coj_citation="Fla. Stat. § 55.05",
    coj_citation_url="https://www.flsenate.gov/Laws/Statutes/2018/Chapter55/All",
    # § 55.05 is a historic FL statute (predates MCAs by ~century); no
    # specific amendment / effective date quoted in the dossier.
    coj_amendment_bill=None,
    coj_effective_date=None,
    requires_unaltered_disclosure_transmission=False,
    transmission_record_retention_years=0,
    broker_compensation_disclosure_required=False,
    broker_advance_fees_prohibited=True,
    broker_advertisement_address_disclosure_required=True,
    private_right_of_action=False,
    enforcement_authority="Florida Attorney General (exclusive)",
    notes=(
        "FCFDL is content-based, not form-prescribed — no row/column "
        "table required; six required content items per § 559.9613(2). "
        "No APR disclosure required (lighter than CA / NY / GA). "
        "Lease financing is NOT covered (narrower scope than CA/NY). "
        "AEGIS as broker: NO upfront broker fees from FL merchants "
        "(§ 559.9614(1)(a)); address + phone disclosure required in any "
        "advertisement (§ 559.9614(3)). CoJ banned by historic § 55.05; "
        "FL Supreme Court in Trauger v. AJ Spagnol Lumber, 442 So.2d 182 "
        "(Fla. 1983) held § 55.05 cannot block enforcement of an "
        "out-of-state CoJ judgment under Full Faith and Credit — but FL-"
        "law CoJs themselves remain void. Penalties: $500/violation, "
        "$20K aggregate (initial); $1,000/violation, $50K aggregate "
        "after written notice. AG-only enforcement; no private right of "
        "action; underlying transaction remains valid even on violation."
    ),
    template_path="fl_fcfdl.html.j2",
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
    "CojStatus",
    "CompliancePolicyError",
    "PendingAmendment",
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
