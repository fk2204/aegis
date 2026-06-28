"""Trade-licensing gate on the dossier "Submit to funders" button.

When a merchant's industry requires state-issued professional or trade
licensure AND we know a verifiable portal for that state+industry, the
dossier replaces the Submit button with a license-verification gate.
The operator must click the official state portal link, look up the
license, and click "✓ Mark license verified" to unlock submission.

Storage posture
---------------
Verification is recorded as an ``audit_log`` row with
``action="merchant.license_verified_manually"`` and
``subject_id=merchant_id``. No new table or migration — the audit log
IS the durable event store for this kind of operator attestation
(matches the pattern of ``merchant.background_checks_complete`` etc).

The ``record_dossier_override`` flow was NOT reused because that path
flips ``documents.parse_status`` (approve/decline), which doesn't fit
a merchant-scoped attestation that touches no document. The closed
``ReasonCode`` literal on the ``overrides`` table also doesn't carry
a "license_verified_manually" value and adding one would require a
migration (the closed-set CHECK in migration 017).

When the gate fires
-------------------
ALL THREE must be true:

1. ``merchant.industry_naics`` is a key in ``LICENSED_INDUSTRIES_BY_NAICS``
2. ``(merchant.state, industry_key)`` is a key in ``LICENSE_PORTALS``
   with a non-empty URL (i.e. we know HOW to verify)
3. No prior ``merchant.license_verified_manually`` audit row exists
   for this merchant

If ANY of the three is false the gate is skipped (``required=False``)
and the original Submit button renders unchanged. Per AEGIS operating-
principle 4, we prefer "skip the gate" over "block with an unverifiable
URL" — a wrong portal link is worse than no gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from aegis.audit import AuditLog

# ---------------------------------------------------------------------------
# NAICS → licensed-industry key
# ---------------------------------------------------------------------------

# Industries that REQUIRE state-issued license in at least some
# jurisdictions. Keys are 6-digit NAICS codes; values are stable string
# keys consumed by ``LICENSE_PORTALS`` lookups + ``LICENSE_INDUSTRY_LABELS``.
#
# Coverage scope: trades, health professions, legal/finance, alcohol,
# childcare, pharmacy. NOT in scope: industries that are universally
# license-free (most retail, software, generic professional services).
#
# Per AEGIS operating-principle 4: we don't pad this list with NAICS
# we haven't verified — every entry below was cross-checked against
# at least one state's licensing-board public matrix.
LICENSED_INDUSTRIES_BY_NAICS: Final[dict[str, str]] = {
    # ---- Construction / specialty trades (NAICS 23) ----
    "236115": "general_contractor",  # New single-family housing (residential)
    "236116": "general_contractor",  # New multifamily housing
    "236117": "general_contractor",  # New housing for-sale builders
    "236118": "general_contractor",  # Residential remodelers
    "236210": "general_contractor",  # Industrial building construction
    "236220": "general_contractor",  # Commercial / institutional
    "238110": "concrete_contractor",
    "238120": "structural_steel_contractor",
    "238130": "framing_contractor",
    "238140": "masonry_contractor",
    "238150": "glass_contractor",
    "238160": "roofing_contractor",
    "238170": "siding_contractor",
    "238190": "foundation_contractor",
    "238210": "electrician",
    "238220": "hvac_plumbing_contractor",  # Plumbing/heating/AC combined
    "238290": "other_building_equipment_contractor",  # elevator, etc.
    "238310": "drywall_contractor",
    "238320": "painting_contractor",
    "238330": "flooring_contractor",
    "238340": "tile_contractor",
    "238350": "finish_carpentry_contractor",
    "238390": "other_finishing_contractor",
    "238910": "site_preparation_contractor",
    "238990": "other_specialty_trade_contractor",
    # ---- Health professions (NAICS 62) ----
    "621111": "physician",
    "621112": "physician",  # Mental health specialists
    "621210": "dentist",
    "621310": "chiropractor",
    "621320": "optometrist",
    "621330": "mental_health_practitioner",
    "621340": "physical_therapist",
    "621391": "podiatrist",
    "621399": "other_health_practitioner",
    "621410": "family_planning_center",
    "621498": "outpatient_care_center",
    "621511": "medical_diagnostic_lab",
    "621610": "home_health_care",
    "621910": "ambulance_service",
    "622110": "hospital",
    "623110": "nursing_care_facility",
    # ---- Legal / finance (NAICS 54, 52) ----
    "541110": "attorney",
    "541211": "cpa",
    "541213": "tax_preparer",
    "522291": "consumer_lender",
    "524210": "insurance_agency",
    "531210": "real_estate_broker",
    # ---- Beauty / personal services (NAICS 81) ----
    "812112": "cosmetology",  # Beauty salons
    "812113": "cosmetology",  # Nail salons
    "812199": "cosmetology",  # Other personal care
    # ---- Food service / alcohol (NAICS 72) ----
    "722410": "alcohol_retailer",  # Drinking places (bars)
    # ---- Childcare (NAICS 62) ----
    "624410": "child_care_facility",
    "624110": "child_youth_services",
    # ---- Pharmacy (NAICS 44) ----
    "446110": "pharmacy",
}


# Operator-facing display labels for each industry key. Stable text —
# changing a label changes UI copy + audit row details.
LICENSE_INDUSTRY_LABELS: Final[dict[str, str]] = {
    "general_contractor": "General Contractor",
    "concrete_contractor": "Concrete Contractor",
    "structural_steel_contractor": "Structural Steel Contractor",
    "framing_contractor": "Framing Contractor",
    "masonry_contractor": "Masonry Contractor",
    "glass_contractor": "Glass Contractor",
    "roofing_contractor": "Roofing Contractor",
    "siding_contractor": "Siding Contractor",
    "foundation_contractor": "Foundation Contractor",
    "electrician": "Electrician",
    "hvac_plumbing_contractor": "HVAC / Plumbing Contractor",
    "other_building_equipment_contractor": "Building Equipment Contractor",
    "drywall_contractor": "Drywall Contractor",
    "painting_contractor": "Painting Contractor",
    "flooring_contractor": "Flooring Contractor",
    "tile_contractor": "Tile / Terrazzo Contractor",
    "finish_carpentry_contractor": "Finish Carpentry Contractor",
    "other_finishing_contractor": "Building Finishing Contractor",
    "site_preparation_contractor": "Site Preparation Contractor",
    "other_specialty_trade_contractor": "Specialty Trade Contractor",
    "physician": "Physician",
    "dentist": "Dentist",
    "chiropractor": "Chiropractor",
    "optometrist": "Optometrist",
    "mental_health_practitioner": "Mental Health Practitioner",
    "physical_therapist": "Physical / Occupational / Speech Therapist",
    "podiatrist": "Podiatrist",
    "other_health_practitioner": "Health Practitioner",
    "family_planning_center": "Family Planning Center",
    "outpatient_care_center": "Outpatient Care Center",
    "medical_diagnostic_lab": "Medical / Diagnostic Laboratory",
    "home_health_care": "Home Health Care Provider",
    "ambulance_service": "Ambulance Service",
    "hospital": "Hospital",
    "nursing_care_facility": "Nursing Care Facility",
    "attorney": "Attorney",
    "cpa": "CPA",
    "tax_preparer": "Tax Preparer",
    "consumer_lender": "Consumer Lender",
    "insurance_agency": "Insurance Agency",
    "real_estate_broker": "Real Estate Broker",
    "cosmetology": "Cosmetology / Personal Care",
    "alcohol_retailer": "Alcohol Retailer",
    "child_care_facility": "Child Care Facility",
    "child_youth_services": "Child / Youth Services",
    "pharmacy": "Pharmacy",
}


# State USPS code → full display name.  Used for the gate banner copy
# ("Search Florida licensing portal").  Limited to the 5 top-volume
# states plus DC + other states that appear in ``LICENSE_PORTALS``.
STATE_NAMES: Final[dict[str, str]] = {
    "FL": "Florida",
    "TX": "Texas",
    "CA": "California",
    "NY": "New York",
    "GA": "Georgia",
    "IL": "Illinois",
    "PA": "Pennsylvania",
    "OH": "Ohio",
    "NC": "North Carolina",
    "VA": "Virginia",
    "WA": "Washington",
    "DC": "District of Columbia",
}


# ---------------------------------------------------------------------------
# State + industry → official licensing portal URL
# ---------------------------------------------------------------------------

# Verified portal URLs for the 5 highest-volume Commera states crossed
# with the 5 most common trade categories.  Each entry was looked up
# against the live state portal at the time of writing; missing entries
# (the ``# TODO`` lines) intentionally leave the combo to skip the
# gate rather than block with an unverifiable link.
#
# State-specific notes:
#   * **TX**: residential plumbing licensed by TSBPE (separate from
#     TDLR which handles HVAC + electrical).  No state-level residential
#     GC license — that's city/county.
#   * **NY**: trade licensing is jurisdiction-specific (NYC DOB,
#     Suffolk County DCA, etc).  No statewide portal for trades; only
#     professional licenses (attorneys, CPAs, doctors) have a state
#     portal at NY DOS.
#   * **GA**: most trades centralized at the Secretary of State
#     Professional Licensing Boards portal.  Residential roofing is
#     NOT licensed statewide.
LICENSE_PORTALS: Final[dict[tuple[str, str], str]] = {
    # ----- Florida (myfloridalicense.com central portal) -----
    ("FL", "general_contractor"): "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
    ("FL", "hvac_plumbing_contractor"): (
        "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name"
    ),
    ("FL", "electrician"): "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
    ("FL", "roofing_contractor"): "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
    ("FL", "cosmetology"): "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
    ("FL", "real_estate_broker"): "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
    ("FL", "alcohol_retailer"): "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
    ("FL", "pharmacy"): "https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
    # ----- Texas (TDLR + specialty boards) -----
    ("TX", "hvac_plumbing_contractor"): (
        "https://www.tdlr.texas.gov/LicenseSearch/SearchACR.asp"
    ),  # TDLR handles HVAC; plumbing has its own TSBPE board (see below).
    ("TX", "electrician"): "https://www.tdlr.texas.gov/LicenseSearch/SearchECR.asp",
    # GC residential is municipal in TX — TODO: confirm if a commercial
    # GC state portal exists; for now don't gate.
    # Roofing isn't state-licensed in TX — leave out so gate doesn't fire.
    ("TX", "real_estate_broker"): "https://www.trec.texas.gov/license-holder-search",
    ("TX", "cosmetology"): "https://www.tdlr.texas.gov/LicenseSearch/SearchCOS.asp",
    # ----- California (CSLB for contractors, DRE for real estate) -----
    ("CA", "general_contractor"): (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
    ),
    ("CA", "hvac_plumbing_contractor"): (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
    ),
    ("CA", "electrician"): (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
    ),
    ("CA", "roofing_contractor"): (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
    ),
    ("CA", "concrete_contractor"): (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
    ),
    ("CA", "painting_contractor"): (
        "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
    ),
    ("CA", "real_estate_broker"): "https://www2.dre.ca.gov/PublicASP/pplinfo.asp",
    ("CA", "cosmetology"): "https://www.barbercosmo.ca.gov/online_services/index.shtml",
    # ----- New York (NY DOS for some professions; trades are jurisdiction-specific) -----
    # NY trade licensing is jurisdiction-specific (NYC DOB, county DCAs)
    # — no state-wide trade portal.  Only state-issued professional
    # licenses (attorneys, real estate, cosmetology) get gated here.
    ("NY", "real_estate_broker"): "https://appext20.dos.ny.gov/lcns_public/chk_load",
    ("NY", "cosmetology"): "https://appext20.dos.ny.gov/lcns_public/chk_load",
    ("NY", "attorney"): "https://iapps.courts.state.ny.us/attorneyservices/search",
    # TODO: confirm portal URLs for NY HVAC / GC / roofing (jurisdiction
    # varies — gate stays off until a per-jurisdiction lookup ships).
    # ----- Georgia (GA SOS PLB) -----
    ("GA", "general_contractor"): "https://verify.sos.ga.gov/verification/",
    ("GA", "hvac_plumbing_contractor"): "https://verify.sos.ga.gov/verification/",
    ("GA", "electrician"): "https://verify.sos.ga.gov/verification/",
    ("GA", "cosmetology"): "https://verify.sos.ga.gov/verification/",
    ("GA", "real_estate_broker"): "https://grec.state.ga.us/grec/grec-publicrecords.aspx",
    # TODO: confirm GA roofing portal (residential roofing not licensed
    # statewide; gate stays off).
}


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LicenseGateContext:
    """Render context for the dossier license-gate banner.

    ``required=False`` means the dossier renders the Submit button
    unchanged. ``required=True`` means the dossier replaces the Submit
    button with the gate banner, and individual per-funder submit
    buttons render in a disabled state with explanatory tooltip.
    """

    required: bool
    industry_key: str | None
    industry_label: str | None
    portal_url: str | None
    state_name: str | None
    already_verified: bool


_LICENSE_VERIFIED_AUDIT_ACTION: Final[str] = "merchant.license_verified_manually"


def _is_already_verified(audit: AuditLog, merchant_id: UUID) -> bool:
    """Return True when this merchant carries a prior license-verified
    audit row. Used to bypass the gate on second render after the
    operator has clicked "Mark license verified"."""
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant_id,
        action=_LICENSE_VERIFIED_AUDIT_ACTION,
        limit=10,
    )
    return bool(rows)


def evaluate_license_gate(
    *,
    merchant_id: UUID,
    state: str | None,
    industry_naics: str | None,
    audit: AuditLog,
) -> LicenseGateContext:
    """Decide whether to render the license-verification gate.

    All three preconditions must hold for the gate to fire:

    1. ``industry_naics`` is in the licensed-industry map
    2. (``state``, industry_key) has a portal URL
    3. No prior operator verification exists on the audit log

    Missing any one → gate is skipped (``required=False``), Submit
    button renders unchanged. The skip-rather-than-block design is
    deliberate per AEGIS operating-principle 4: better to NOT block
    when we can't verify than to block with a wrong portal link.
    """
    if not industry_naics:
        return LicenseGateContext(
            required=False,
            industry_key=None,
            industry_label=None,
            portal_url=None,
            state_name=None,
            already_verified=False,
        )

    industry_key = LICENSED_INDUSTRIES_BY_NAICS.get(industry_naics)
    if industry_key is None:
        return LicenseGateContext(
            required=False,
            industry_key=None,
            industry_label=None,
            portal_url=None,
            state_name=None,
            already_verified=False,
        )

    state_upper = (state or "").upper()
    portal_url = LICENSE_PORTALS.get((state_upper, industry_key))
    if not portal_url:
        # We know the industry needs a license but don't have a verified
        # portal URL for this state. Skip the gate rather than block
        # with an unverifiable link. The industry_label still surfaces
        # for diagnostic / future-coverage tracking.
        return LicenseGateContext(
            required=False,
            industry_key=industry_key,
            industry_label=LICENSE_INDUSTRY_LABELS.get(industry_key),
            portal_url=None,
            state_name=STATE_NAMES.get(state_upper),
            already_verified=False,
        )

    already_verified = _is_already_verified(audit, merchant_id)
    return LicenseGateContext(
        required=not already_verified,
        industry_key=industry_key,
        industry_label=LICENSE_INDUSTRY_LABELS.get(industry_key),
        portal_url=portal_url,
        state_name=STATE_NAMES.get(state_upper, state_upper),
        already_verified=already_verified,
    )


def record_license_verification(
    *,
    merchant_id: UUID,
    state: str | None,
    industry_naics: str | None,
    actor: str,
    actor_email: str | None,
    audit: AuditLog,
) -> None:
    """Persist the operator's "Mark license verified" attestation.

    Single audit row written; second render of the dossier picks it up
    via ``_is_already_verified`` and renders the Submit button.
    Audit-write failure propagates per CLAUDE.md compliance rule.
    """
    industry_key = LICENSED_INDUSTRIES_BY_NAICS.get(industry_naics) if industry_naics else None
    audit.record(
        actor=actor,
        actor_email=actor_email,
        action=_LICENSE_VERIFIED_AUDIT_ACTION,
        subject_type="merchant",
        subject_id=merchant_id,
        details={
            "industry_naics": industry_naics,
            "industry_key": industry_key,
            "industry_label": (LICENSE_INDUSTRY_LABELS.get(industry_key) if industry_key else None),
            "state": (state or "").upper() or None,
        },
    )


__all__ = [
    "LICENSED_INDUSTRIES_BY_NAICS",
    "LICENSE_INDUSTRY_LABELS",
    "LICENSE_PORTALS",
    "STATE_NAMES",
    "LicenseGateContext",
    "evaluate_license_gate",
    "record_license_verification",
]
