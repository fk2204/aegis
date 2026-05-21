"""Pure-function translation between Close Lead payloads and AEGIS merchants.

No HTTP, no DB, no Pydantic-settings reads. Pure dict-in, dict-out.
The Close client (step 2) hands raw Lead JSON to functions here; the
sync orchestrator (step 5) takes the output and upserts ``merchants``.

Conventions
-----------
* Close custom-field keys appear in the Lead payload as
  ``custom.cf_<field_id>``. The IDs for the Commera org are in
  ``CLOSE_FIELD_IDS`` below — single source of truth, operator-reviewable.
* Choice fields can carry Close's ``"-None-"`` sentinel; we treat that
  (plus empty string and missing keys) as null.
* Money values arrive as operator-typed text from Close — string-cleaned
  via ``parse_money`` to ``Decimal``. Never ``float``.
* Anything we cannot parse raises ``FieldMapError`` with the original
  value in the message. No silent defaults — the operator must see
  surprises in the audit trail and fix them in Close.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from aegis.logger import get_logger

if TYPE_CHECKING:
    from aegis.audit import AuditLog

_log = get_logger(__name__)


# ----------------------------------------------------------------------
# Operator-reviewable constants
# ----------------------------------------------------------------------


# Close custom-field IDs from the Commera org. Source-of-truth:
# `find_lead_custom_fields` MCP query on 2026-05-20. If Close renames
# a field or the operator adds a duplicate, update here.
#
# Two notes worth flagging:
#  * "entity_type_a" / "entity_type_b" — Close has TWO Entity-type
#    custom fields ("Entity type" and "Entity_type") that drifted
#    over time. resolve_entity_type() handles the reconciliation.
#  * "fico_score" (number) and "fico_range" (choice) both exist —
#    we use fico_range as the primary signal since the operator's
#    Close ingestion fills the range bucket more reliably.
CLOSE_FIELD_IDS: Final[dict[str, str]] = {
    "legal_name":              "cf_Atu3WT1FlUIPEHHZ5ryMJbmgGQiHZjBCptOg2YMwXWi",
    "dba_name":                "cf_YcldmRoTpfdqpG16JTZ7Jq3is3wgNW2P6YwAIkCAwuS",
    "ein":                     "cf_ik3aCHe67NWDzn1DeE3Q2HUbR0vFJxTcTS1PhkloOAN",
    "owner_name":              "cf_CAGnfwW3PmzK52wzjYsgLQeqSogEQPJj5z1w63E4IhC",
    "state":                   "cf_lA3zOyNn28vtEPiKKx2SKGvE57sQRndIdVETtZUmzZZ",
    "industry":                "cf_Wls6nOfOp8CE8VNp4KkJxfZTSYlkqByKHkRwa0VQelr",
    "naics_code":              "cf_SwEqRSTvhlM4zPCm0LsGNECUzOsrysqyIMdbdLoUmGD",
    "time_in_business_months": "cf_mCyntewx8FYBCtiW3NMv3HJwT7bDXwsbfOJTqoq3ClY",
    "fico_range":              "cf_cFV00H5FFZ5Sw55JBKFskDo5HjgEpIKKBq9bkiac1kP",
    "requested_amount":        "cf_TVx0D6Cx8qg9Dey7LgSgQqLIosZnGcYukKH0ImVuvw4",
    "entity_type_a":           "cf_FwBTNyt2ux6OnVIoRWIn0qkjXsVpAI0d4Wy1vjPoO4V",  # "Entity type"
    "entity_type_b":           "cf_sAtWGtaP7eqj5QYH8D91Vc1kX788DRVwHh6Ydufy3ca",  # "Entity_type"
}


# FICO Range → integer lower-bound. Baked per design doc decision #5
# (lower-bound conservative, not configurable). If the operator wants
# midpoint later, that's a one-commit code change.
FICO_RANGE_LOWER_BOUND: Final[dict[str, int]] = {
    "<550":    549,
    "550-599": 550,
    "600-649": 600,
    "650-699": 650,
    "700+":    700,
}


# Industry → 6-digit NAICS 2022 lookup. Static table; operator validates
# during build (this is one of the items called out as a binding
# decision in docs/research/close-integration-design.md). Where the
# operator's category is too broad to pick a single NAICS code
# unambiguously, we pick the closest "all other / general" sub-category
# in that sector — the operator's NAICS Code field overrides this
# derived value when set explicitly.
#
# 18 entries match Close's "Industry" choice list verbatim (find_lead_
# custom_fields MCP query, 2026-05-20). Adding a new Industry choice
# in Close without adding it here will raise FieldMapError at parse
# time — never silently defaults.
CLOSE_INDUSTRY_TO_NAICS: Final[dict[str, str]] = {
    "Auto Repair / Service":               "811111",  # General Automotive Repair
    "Beauty / Salon / Spa":                "812112",  # Beauty Salons
    "Construction — General Contractor":   "236220",  # Commercial+Institutional Bldg Construction
    "Construction — Specialty Trades":     "238990",  # All Other Specialty Trade Contractors
    "Fitness / Gym":                       "713940",  # Fitness and Recreational Sports Centers
    "Healthcare — Dental":                 "621210",  # Offices of Dentists
    "Healthcare — Medical Practice":       "621111",  # Offices of Physicians
    "Healthcare — Veterinary":             "541940",  # Veterinary Services
    "Hospitality / Hotel":                 "721110",  # Hotels and Motels
    "Manufacturing":                       "339999",  # All Other Miscellaneous Manufacturing
    "Other (Approved)":                    "999999",  # Sentinel — NAICS has no generic "other"
    "Professional Services":               "541990",  # All Other Prof/Sci/Tech Services
    "Real Estate Services":                "531390",  # Other Activities Related to Real Estate
    "Restaurant / Food Service":           "722511",  # Full-Service Restaurants
    "Retail — General":                    "459999",  # All Other Miscellaneous Retailers
    "Retail — Specialty":                  "459999",  # Same — operator's NAICS Code field refines
    "Trucking / Logistics":                "484110",  # General Freight Trucking, Local
    "Wholesale / Distribution":            "423990",  # Other Miscellaneous Durable Goods Wholesale
}


# Close's "no selection" choice value. Treated as null everywhere.
_NONE_MARKER: Final[str] = "-None-"


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


class FieldMapError(ValueError):
    """Raised on parse failures — money garbage, unknown industry,
    out-of-band FICO. Always carries the original value in the message
    so the operator can spot what came in from Close."""


# ----------------------------------------------------------------------
# Pure parsers
# ----------------------------------------------------------------------


def parse_fico_range(value: str | None) -> int | None:
    """Close ``FICO Range`` choice → integer lower-bound.

    None / empty → None. Anything not in ``FICO_RANGE_LOWER_BOUND``
    → ``FieldMapError`` (including values that look like new bands
    Close hasn't published — better to fail loud than guess).
    """
    if value is None or value == "" or value == _NONE_MARKER:
        return None
    if value not in FICO_RANGE_LOWER_BOUND:
        raise FieldMapError(
            f"unknown FICO Range value {value!r}; expected one of "
            f"{sorted(FICO_RANGE_LOWER_BOUND.keys())}"
        )
    return FICO_RANGE_LOWER_BOUND[value]


def industry_to_naics(industry: str | None) -> str | None:
    """Close ``Industry`` choice → 6-digit NAICS code.

    None / empty / "-None-" → None.
    Unknown industry → ``FieldMapError`` (never silent default — the
    operator must see and decide whether to add the mapping).
    """
    if industry is None or industry == "" or industry == _NONE_MARKER:
        return None
    if industry not in CLOSE_INDUSTRY_TO_NAICS:
        raise FieldMapError(
            f"unknown Close Industry value {industry!r}; "
            f"add it to CLOSE_INDUSTRY_TO_NAICS in close/field_map.py"
        )
    return CLOSE_INDUSTRY_TO_NAICS[industry]


def parse_money(value: str | int | float | None) -> Decimal | None:
    """Operator-typed money text → ``Decimal``.

    Strips ``$`` and ``,`` formatting; preserves the operator's
    decimal-place precision. Examples::

      "$1,500.00" → Decimal("1500.00")
      "1500"      → Decimal("1500")
      "$1,500.5"  → Decimal("1500.5")     # NOT normalized to "1500.50"
      ""          → None
      None        → None
      "garbage"   → FieldMapError
      "1.2.3"     → FieldMapError

    Never constructs ``Decimal`` from a binary ``float`` — all inputs
    are coerced through ``str()`` first, satisfying CLAUDE.md's
    "``Decimal("1.10")`` works; ``Decimal(1.10)`` doesn't" rule.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    if cleaned == "":
        raise FieldMapError(
            f"money value collapsed to empty after $ / comma strip: "
            f"{value!r}"
        )
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise FieldMapError(
            f"could not parse money value {value!r} (after strip: "
            f"{cleaned!r}); decimal.InvalidOperation"
        ) from exc


def resolve_entity_type(
    *,
    entity_type_a: str | None,
    entity_type_b: str | None,
    close_lead_id: str,
    audit: AuditLog | None = None,
) -> str | None:
    """Reconcile Close's two ``Entity type`` / ``Entity_type`` fields.

    Returns the non-null value when only one is set. Returns the
    agreed value when both are set and match. When both are set AND
    they disagree, writes one audit row with action
    ``close.field_map.entity_type_conflict`` (when an ``AuditLog`` is
    injected) AND returns ``entity_type_a`` — the operator must
    reconcile in Close. Audit-write failure is logged but never
    raised (the conflict signal is itself non-blocking; the field-map
    result still flows downstream).

    Treats ``"-None-"`` (Close's "no selection" choice value) and
    empty string as null.
    """
    a = _strip_none_marker(entity_type_a)
    b = _strip_none_marker(entity_type_b)

    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    if a == b:
        return a

    # Both set, disagree.
    if audit is not None:
        try:
            audit.record(
                actor="close_field_map",
                action="close.field_map.entity_type_conflict",
                details={
                    "close_lead_id": close_lead_id,
                    "entity_type_a": a,
                    "entity_type_b": b,
                    "resolved_to": a,
                },
            )
        except Exception:
            # Don't let the audit write failure mask the field-map
            # result. The logger entry below is the secondary signal.
            _log.warning(
                "close.field_map.entity_type_conflict_audit_failed",
                exc_info=True,
            )
    _log.warning(
        "close.field_map.entity_type_conflict lead=%s a=%s b=%s",
        close_lead_id,
        a,
        b,
    )
    return a


def _strip_none_marker(value: str | None) -> str | None:
    """Treat Close's ``-None-`` sentinel and empty string as null."""
    if value is None or value == _NONE_MARKER or value == "":
        return None
    return value


# ----------------------------------------------------------------------
# Custom-field accessor (helper for sync.py in step 5; safe + pure)
# ----------------------------------------------------------------------


def get_custom_field(
    payload: dict[str, Any], aegis_field_name: str
) -> Any:  # noqa: ANN401 — Close custom-field values are heterogeneous
    """Pull a custom-field value out of a Close Lead payload.

    Close puts custom fields under keys of the form
    ``custom.cf_<id>``. This helper maps an AEGIS-side name
    (e.g. ``"fico_range"``) through ``CLOSE_FIELD_IDS`` and returns
    the raw value (or None if absent). Lets the sync orchestrator stay
    out of the cf_id business.
    """
    if aegis_field_name not in CLOSE_FIELD_IDS:
        raise FieldMapError(
            f"unknown AEGIS-side field name {aegis_field_name!r}; "
            f"add it to CLOSE_FIELD_IDS in close/field_map.py"
        )
    cf_id = CLOSE_FIELD_IDS[aegis_field_name]
    return payload.get(f"custom.{cf_id}")


__all__ = [
    "CLOSE_FIELD_IDS",
    "CLOSE_INDUSTRY_TO_NAICS",
    "FICO_RANGE_LOWER_BOUND",
    "FieldMapError",
    "get_custom_field",
    "industry_to_naics",
    "parse_fico_range",
    "parse_money",
    "resolve_entity_type",
]
