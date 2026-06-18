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
# Close OPPORTUNITY custom-field IDs. Same shape as
# ``CLOSE_FIELD_IDS`` above (Lead-side fields). Separated by table so
# the read / write helpers can't accidentally cross the streams — a
# ``custom.<lead-cf>`` key won't resolve on an Opportunity payload and
# vice versa.
#
# Source-of-truth: ``find_opportunity_custom_fields`` MCP query on
# 2026-06-15. Operator-confirmed mapping for the AEGIS offer-sync:
#   * recommended_amount  → ``Suggested Max Advance``
#   * factor_rate         → ``Recommended Factor Rate``
#   * holdback_pct        → ``Recommended Holdback Pct``
#   * true_revenue        → ``True Revenue``        (monthly normalised)
#   * holdback_capacity   → ``Holdback Capacity``   (operator budget,
#                                                    monthly $)
#   * mca_position_count  → ``Existing MCA Debits Identified``
#                            (count of distinct funder counterparties)
#   * mca_daily_total     → ``Existing MCA Daily Debits Total``
#                            (avg daily MCA debit total, $)
CLOSE_OPPORTUNITY_FIELD_IDS: Final[dict[str, str]] = {
    "suggested_max_advance": "cf_XMtBI8Of38icbdX7ywUhFb0ENAUCxat6S9AqfGcOSjg",
    "recommended_factor_rate": "cf_flsrZT0fTjNohgrRElCNIUtV0SROfw20FVnB6pXA7Ox",
    "recommended_holdback_pct": "cf_mJxOH8wrNd4K4omsR5I6TmI8OU86HidIbQB780VNJRD",
    "true_revenue": "cf_DivasTofPYPOdmFWnuLbts4PRTMKWZIrT9q56x698lu",
    "holdback_capacity": "cf_B3gXaET1Hzhfffdvd453VXEMYPDUuB3FYsXEMGTmDQI",
    "existing_mca_count": "cf_exJtKEjItwsJ5fr9k7bEzgrwArBwggrDnCsyMZEK5gr",
    "existing_mca_daily_total": "cf_xOiXpJtf1W9DDFBrpQ5DNoYWFCQRSeemfXXMEqtrveM",
}


CLOSE_FIELD_IDS: Final[dict[str, str]] = {
    # Inbound: Lead identity + business profile (read by /webhooks/close)
    "legal_name": "cf_Atu3WT1FlUIPEHHZ5ryMJbmgGQiHZjBCptOg2YMwXWi",
    "dba_name": "cf_YcldmRoTpfdqpG16JTZ7Jq3is3wgNW2P6YwAIkCAwuS",
    "ein": "cf_ik3aCHe67NWDzn1DeE3Q2HUbR0vFJxTcTS1PhkloOAN",
    "owner_name": "cf_CAGnfwW3PmzK52wzjYsgLQeqSogEQPJj5z1w63E4IhC",
    "state": "cf_lA3zOyNn28vtEPiKKx2SKGvE57sQRndIdVETtZUmzZZ",
    "industry": "cf_Wls6nOfOp8CE8VNp4KkJxfZTSYlkqByKHkRwa0VQelr",
    "naics_code": "cf_SwEqRSTvhlM4zPCm0LsGNECUzOsrysqyIMdbdLoUmGD",
    "time_in_business_months": "cf_mCyntewx8FYBCtiW3NMv3HJwT7bDXwsbfOJTqoq3ClY",
    "fico_range": "cf_cFV00H5FFZ5Sw55JBKFskDo5HjgEpIKKBq9bkiac1kP",
    "requested_amount": "cf_TVx0D6Cx8qg9Dey7LgSgQqLIosZnGcYukKH0ImVuvw4",
    "entity_type_a": "cf_FwBTNyt2ux6OnVIoRWIn0qkjXsVpAI0d4Wy1vjPoO4V",  # "Entity type"
    "entity_type_b": "cf_sAtWGtaP7eqj5QYH8D91Vc1kX788DRVwHh6Ydufy3ca",  # "Entity_type"
    # Outbound: Aegis-* fields written by close.sync.push_decision_to_close
    "aegis_applicant_id": "cf_HJbCNJ2k7X4lKOPvl2Bql1FkTHDeGfS9zJL6dDafUd8",
    "aegis_score": "cf_0aYBJLLYHM4isq2CxyywRf6Syw5Mwe82hJvcOx3HRVW",
    "aegis_recommendation": "cf_hyhD8buv0SxuKCa5JsRafCXSlVz565nilXcXKxWbFBq",
    "ofac_status": "cf_I8Csenfde4IdiNnSEPprhL7hzzmtiGW6HYIdDAg25PB",
    "aegis_last_synced": "cf_W2M1v1giWa3bzx7lxBlJstda8708Ad2RMCBb9Cbt94d",
}


# FICO Range → integer lower-bound. Baked per design doc decision #5
# (lower-bound conservative, not configurable). If the operator wants
# midpoint later, that's a one-commit code change.
FICO_RANGE_LOWER_BOUND: Final[dict[str, int]] = {
    "<550": 549,
    "550-599": 550,
    "600-649": 600,
    "650-699": 650,
    "700+": 700,
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
#
# PLACEHOLDER NOTE — the following four mappings are pre-revenue
# placeholders, chosen with no actual deal-flow data:
#   "Manufacturing"                        ("339999")
#   "Construction — General Contractor"    ("236220")
#   "Retail — General" / "Retail — Specialty" (both "459999")
#   "Other (Approved)"                     ("999999" — sentinel)
# Revisit after 30 days of real Close-routed deals to align with
# Commera's actual merchant mix.
CLOSE_INDUSTRY_TO_NAICS: Final[dict[str, str]] = {
    "Auto Repair / Service": "811111",  # General Automotive Repair
    "Beauty / Salon / Spa": "812112",  # Beauty Salons
    "Construction — General Contractor": "236220",  # Commercial+Institutional Bldg Construction
    "Construction — Specialty Trades": "238990",  # All Other Specialty Trade Contractors
    "Fitness / Gym": "713940",  # Fitness and Recreational Sports Centers
    "Healthcare — Dental": "621210",  # Offices of Dentists
    "Healthcare — Medical Practice": "621111",  # Offices of Physicians
    "Healthcare — Veterinary": "541940",  # Veterinary Services
    "Hospitality / Hotel": "721110",  # Hotels and Motels
    "Manufacturing": "339999",  # All Other Miscellaneous Manufacturing
    "Other (Approved)": "999999",  # Sentinel — NAICS has no generic "other"
    "Professional Services": "541990",  # All Other Prof/Sci/Tech Services
    "Real Estate Services": "531390",  # Other Activities Related to Real Estate
    "Restaurant / Food Service": "722511",  # Full-Service Restaurants
    "Retail — General": "459999",  # All Other Miscellaneous Retailers
    "Retail — Specialty": "459999",  # Same — operator's NAICS Code field refines
    "Trucking / Logistics": "484110",  # General Freight Trucking, Local
    "Wholesale / Distribution": "423990",  # Other Miscellaneous Durable Goods Wholesale
}


# Close's "no selection" choice value. Treated as null everywhere.
_NONE_MARKER: Final[str] = "-None-"


# Close "Entity type" / "Entity_type" choice → AEGIS EntityType literal.
# Source: find_lead_custom_fields MCP query on 2026-05-20. Maps every
# Close-published choice to the matching AEGIS-side token. Unknown
# values raise FieldMapError — never silently bucketed into "other".
#
# Note: AEGIS's EntityType does not currently distinguish C-Corp from
# S-Corp; both collapse to "corp". If that distinction starts to matter
# (it doesn't for any compliance rule today), extend MerchantRow first.
CLOSE_ENTITY_TYPE_TO_AEGIS: Final[dict[str, str]] = {
    "LLC": "llc",
    "C-Corp": "corp",
    "S-Corp": "corp",
    "Sole Proprietorship": "sole_prop",
    "Partnership": "partnership",
    "Non-Profit": "other",
    "Other": "other",
    "Option 1": "other",  # Stale Close template choice; treat as other
}


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
        raise FieldMapError(f"money value collapsed to empty after $ / comma strip: {value!r}")
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


def normalize_entity_type(value: str | None) -> str | None:
    """Close ``Entity type`` choice → AEGIS ``EntityType`` literal.

    None / "" / "-None-" → None.
    Unknown choice → ``FieldMapError`` (so the operator sees Close
    drift rather than silently bucketing into "other").
    """
    stripped = _strip_none_marker(value)
    if stripped is None:
        return None
    if stripped not in CLOSE_ENTITY_TYPE_TO_AEGIS:
        raise FieldMapError(
            f"unknown Close Entity type value {stripped!r}; "
            f"add it to CLOSE_ENTITY_TYPE_TO_AEGIS in close/field_map.py"
        )
    return CLOSE_ENTITY_TYPE_TO_AEGIS[stripped]


# ----------------------------------------------------------------------
# Custom-field accessor (helper for sync.py in step 5; safe + pure)
# ----------------------------------------------------------------------


def get_custom_field(payload: dict[str, Any], aegis_field_name: str) -> Any:  # noqa: ANN401 — Close custom-field values are heterogeneous
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


def extract_lead_description(close_lead_payload: dict[str, Any]) -> str | None:
    """Pull the Close Lead ``description`` field.

    Feature D — used by the merchant-context refresh orchestrator to
    populate ``merchants.close_lead_description``. Lead ``description``
    is a top-level (non-custom-field) field on the Lead object; it sits
    alongside ``display_name`` / ``name`` / ``url`` in the payload.

    Returns ``None`` when:
      * the key is missing entirely (Lead has no description set),
      * the value is JSON ``null``,
      * the value collapses to an empty / whitespace-only string after
        ``strip()``.

    Trims surrounding whitespace; preserves internal newlines /
    formatting so the operator's paragraph structure survives the
    round-trip into the extraction prompt.
    """
    raw = close_lead_payload.get("description")
    if raw is None:
        return None
    if not isinstance(raw, str):
        # Defensive — Close's contract is string, but a surprise should
        # collapse to None rather than raise; the orchestrator is
        # best-effort and a single weird field must not block the
        # refresh.
        return None
    trimmed = raw.strip()
    return trimmed or None


def filename_matches_statement_filter(filename: str, filters: tuple[str, ...]) -> bool:
    """Case-insensitive substring match of ``filename`` against ``filters``.

    Used by ``aegis.workers.process_close_attachments`` to decide whether
    a Close-attached file is a candidate statement (parse it) or not
    (audit ``close.attachment.skipped`` and move on). Substring rather
    than prefix because Close filenames often carry merchant or date
    prefixes (``2025-04_KYC_bank_statement.pdf``, ``acme_stmt_apr.pdf``).

    Empty ``filters`` returns False — operator opt-out via env to
    explicitly disable auto-parsing should NOT silently let everything
    through. They want zero, they get zero.
    """
    if not filters:
        return False
    lowered = filename.lower()
    return any(token in lowered for token in filters)


# Deny list of filename substrings (lowercased). When ANY of these
# appears in an attachment's filename, the file is obviously not a bank
# statement (driver's license, voided check, signed contract, tax
# return, etc.) and we reject it BEFORE paying for download + Bedrock
# extraction.
#
# Lists like this MUST be defined narrowly — false positives waste real
# statements. Each term here was added because the
# ``recover_legacy_docs.py --apply`` pass on 2026-06-16 surfaced it as
# a concrete non-statement filename in the prod Close attachments.
# Operator-curated; extending the list is a one-line code change.
#
# Consumed by both ``aegis.workers.process_close_attachments`` (the
# webhook path) and ``scripts/recover_legacy_docs.py`` (the recovery
# path) so the two surfaces stay in sync. Live in close/field_map.py
# alongside ``filename_matches_statement_filter`` for the same reason
# the existing allow-list filter lives here — both are filename-shape
# decisions about Close-attached files.
NON_STATEMENT_FILENAME_TERMS: tuple[str, ...] = (
    "voided",
    "void check",
    "driver",
    "license",
    "contract",
    "application",
    "bylaws",
    "tax return",
    "balance sheet",
    "p&l",
    "profit",
    "invoice",
    "w-2",
    "1099",
    "signed",
    "agreement",
    "addendum",
    "amendment",
    "load-lift-enterprise-llc",
)


def filename_is_non_statement(filename: str) -> str | None:
    """Return the matched deny-list term when the filename is obviously
    NOT a bank statement, or ``None`` when no deny term matches.

    Case-insensitive substring match against ``NON_STATEMENT_FILENAME_TERMS``.
    A non-None return short-circuits the download + parse paths in the
    callers; the term itself is surfaced in audit / CSV details so the
    operator can see WHY a file was rejected without re-running the
    matcher.

    Used by both the Close webhook orchestration
    (``workers.process_close_attachments``) and the recovery script
    (``scripts/recover_legacy_docs.py``). Both call sites apply this
    AFTER the allow-list check (statement / bank / stmt) so a filename
    that happens to contain a deny term wedged inside a statement-named
    file (e.g. ``Bank Statement Plus Voided Check Cover.pdf``) is still
    correctly rejected — the deny list wins.
    """
    lowered = filename.lower()
    for term in NON_STATEMENT_FILENAME_TERMS:
        if term in lowered:
            return term
    return None


__all__ = [
    "CLOSE_ENTITY_TYPE_TO_AEGIS",
    "CLOSE_FIELD_IDS",
    "CLOSE_INDUSTRY_TO_NAICS",
    "CLOSE_OPPORTUNITY_FIELD_IDS",
    "FICO_RANGE_LOWER_BOUND",
    "NON_STATEMENT_FILENAME_TERMS",
    "FieldMapError",
    "extract_lead_description",
    "filename_is_non_statement",
    "filename_matches_statement_filter",
    "get_custom_field",
    "industry_to_naics",
    "normalize_entity_type",
    "parse_fico_range",
    "parse_money",
    "resolve_entity_type",
]
