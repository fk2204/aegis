"""Push the 3 real AEGIS funders → Zoho Lenders module.

Idempotent: matches existing Zoho Lender records by Name (case-insensitive).
If a Lender with the same Name already exists in Zoho, this UPDATES it;
otherwise INSERTS a new record.

Field mapping (FunderRow → Lender):

  name                        → Name
  Lender_Short_Code (derived) → Lender_Short_Code
  min_monthly_revenue         → Minimum_Monthly_Revenue
  min_avg_daily_balance       → Min_Average_Daily_Balance
  min_credit_score            → Minimum_FICO
  min_months_in_business      → Minimum_Time_in_Business_Months
  max_positions               → Max_Position_Accepted (picklist)
  accepts_stacking            → Accepts_Consolidations
  max_advance                 → Maximum_Funding_Amount
  min_advance                 → Minimum_Funding_Amount
  max_nsf_tolerance           → Max_NSFs_Allowed
  typical_factor_low/high     → Buy_Rate_Low/High
  excluded_industries (str)   → Excluded_Industries (Zoho multiselect, mapped
                                to closest picklist value where possible;
                                unmapped items captured in Internal_Notes)
  notes                       → Internal_Notes (appended)

Run on the box:
    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/_seed_zoho_lenders.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any

from aegis.api.deps import get_funder_repository
from aegis.funders.models import FunderRow
from aegis.zoho.client import ZohoClient

# --- mapping helpers -------------------------------------------------------

_SHORT_CODES: dict[str, str] = {
    "logic advance group": "LAG",
    "velocity capital group": "VCG",
    "swiftsource funding": "SSF",
}

_LENDER_TYPE: dict[str, str] = {
    "logic advance group": "Tier 3 (Sub-prime)",
    "velocity capital group": "Tier 2",
    "swiftsource funding": "Specialty",
}

# Submission contact info pulled directly from the signed ISO packets.
_CONTACTS: dict[str, dict[str, str]] = {
    "logic advance group": {
        "Primary_Contact_Name": "Erik",
        "Primary_Contact_Phone": "(516) 261-2730",
        "Submission_Email": "Submissions@logicadvancegroup.com",
        "Website": "https://logicadvancegroup.com",
    },
    "velocity capital group": {
        "Primary_Contact_Phone": "(833) 824-3863",  # (833) VCG-FUND
        "Primary_Contact_Email": "info@velocitycg.com",
        "Submission_Email": "info@velocitycg.com",
        "Website": "https://www.velocitycg.com",
    },
    "swiftsource funding": {
        "Submission_Email": "Subs@SwiftFunding.net",
        "Escalation_Contact_Email": "Jason@SwiftFunding.net",
    },
}


def _max_position_picklist(max_positions: int | None, name_lower: str) -> str | None:
    """AEGIS max_positions int → Zoho Max_Position_Accepted picklist value."""
    if name_lower == "swiftsource funding":
        # 2nd-and-up only; no upper cap published. "Any" is closest, but
        # note the 1st-position exclusion in notes.
        return "Any"
    if max_positions is None:
        return None
    if max_positions <= 1:
        return "1st Only"
    if max_positions == 2:
        return "Up to 2nd"
    if max_positions == 3:
        return "Up to 3rd"
    if max_positions == 4:
        return "Up to 4th"
    return "Any"


# AEGIS excluded_industries → Zoho Excluded_Industries picklist values.
# Anything not in this map is preserved in Internal_Notes so nothing is
# silently dropped.
_INDUSTRY_MAP: dict[str, str] = {
    "automotive dealership": "Automotive Dealership",
    "real estate": "Real Estate",
    "financial services": "Financial Services",
    "trucking": "Trucking",
    "gas stations": "Gas Station",
    "gas station": "Gas Station",
    "staffing": "Staffing Agency",
    "law firms": "Legal Services",
    "vehicle/auto dealer": "Automotive Dealership",
}


def _map_excluded_industries(industries: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Return (picklist_values, unmapped_industries)."""
    mapped: list[str] = []
    unmapped: list[str] = []
    seen: set[str] = set()
    for ind in industries:
        key = ind.lower().strip()
        pick = _INDUSTRY_MAP.get(key)
        if pick is None:
            unmapped.append(ind)
        elif pick not in seen:
            mapped.append(pick)
            seen.add(pick)
    return mapped, unmapped


def _funder_to_lender_payload(f: FunderRow) -> dict[str, Any]:
    name_lower = f.name.lower()
    excluded_picklist, unmapped_inds = _map_excluded_industries(f.excluded_industries)
    notes_parts: list[str] = []
    if unmapped_inds:
        notes_parts.append(
            "Additional excluded industries (no Zoho picklist match — review "
            "manually): " + ", ".join(unmapped_inds)
        )
    if f.notes:
        notes_parts.append(f.notes)

    payload: dict[str, Any] = {
        "Name": f.name,
        "Lender_Status": "Active" if f.active else "Paused",
    }
    short = _SHORT_CODES.get(name_lower)
    if short:
        payload["Lender_Short_Code"] = short
    lender_type = _LENDER_TYPE.get(name_lower)
    if lender_type:
        payload["Lender_Type"] = lender_type
    if f.min_monthly_revenue is not None:
        payload["Minimum_Monthly_Revenue"] = str(f.min_monthly_revenue)
    if f.min_avg_daily_balance is not None:
        payload["Min_Average_Daily_Balance"] = str(f.min_avg_daily_balance)
    if f.min_credit_score is not None:
        payload["Minimum_FICO"] = f.min_credit_score
    if f.min_months_in_business is not None:
        payload["Minimum_Time_in_Business_Months"] = f.min_months_in_business
    mp = _max_position_picklist(f.max_positions, name_lower)
    if mp:
        payload["Max_Position_Accepted"] = mp
    payload["Accepts_Consolidations"] = bool(f.accepts_stacking)
    # Zoho currency fields are capped at 6 integer digits (max $999,999).
    # Anything larger gets dropped to a notes line so the record still saves.
    _currency_cap = Decimal("999999.99")
    if f.min_advance is not None:
        if f.min_advance <= _currency_cap:
            payload["Minimum_Funding_Amount"] = str(f.min_advance)
        else:
            notes_parts.append(
                f"Minimum funding amount: ${f.min_advance} "
                f"(does not fit Zoho currency precision — widen field to capture)"
            )
    if f.max_advance is not None:
        if f.max_advance <= _currency_cap:
            payload["Maximum_Funding_Amount"] = str(f.max_advance)
        else:
            notes_parts.append(
                f"Maximum funding amount: ${f.max_advance} "
                f"(does not fit Zoho currency precision — widen field to capture)"
            )
    if f.max_nsf_tolerance is not None:
        payload["Max_NSFs_Allowed"] = f.max_nsf_tolerance
    if f.typical_factor_low is not None:
        payload["Buy_Rate_Low"] = float(f.typical_factor_low)
    if f.typical_factor_high is not None:
        payload["Buy_Rate_High"] = float(f.typical_factor_high)
    if excluded_picklist:
        payload["Excluded_Industries"] = excluded_picklist
    contact = _CONTACTS.get(name_lower) or {}
    for k, v in contact.items():
        payload[k] = v
    if notes_parts:
        payload["Internal_Notes"] = "\n\n".join(notes_parts)
    return payload


# --- main ------------------------------------------------------------------


def _find_lender_by_name(client: ZohoClient, name: str) -> str | None:
    """Return Zoho Lender record id matching ``name`` (case-insensitive), or None."""
    try:
        resp = client.request(
            "GET",
            f"/crm/v8/Lenders/search?criteria=(Name:equals:{name})&fields=Name,id",
        )
    except Exception:
        return None
    if not isinstance(resp, dict):
        return None
    data = resp.get("data") or []
    for row in data:
        if isinstance(row, dict) and row.get("id"):
            return str(row["id"])
    return None


def _upsert_lender(client: ZohoClient, payload: dict[str, Any]) -> tuple[str, str]:
    """Insert or update one Lender record. Returns (action, zoho_id)."""
    name = payload["Name"]
    existing_id = _find_lender_by_name(client, name)
    if existing_id is not None:
        resp = client.request(
            "PUT",
            f"/crm/v8/Lenders/{existing_id}",
            json={"data": [payload]},
        )
        return ("UPDATE", existing_id)
    resp = client.request("POST", "/crm/v8/Lenders", json={"data": [payload]})
    if isinstance(resp, dict):
        items = resp.get("data") or []
        if items and isinstance(items[0], dict):
            details = items[0].get("details") or {}
            new_id = details.get("id") if isinstance(details, dict) else None
            if new_id:
                return ("INSERT", str(new_id))
    raise RuntimeError(f"unexpected Zoho POST response: {resp!r}")


def main() -> int:
    client = ZohoClient()
    repo = get_funder_repository()
    funders = [
        f for f in repo.list_active()
        if f.name.lower() in _SHORT_CODES
    ]
    if len(funders) != 3:
        print(
            f"warning: expected 3 named funders (Logic / VCG / Swiftsource) "
            f"in AEGIS; found {len(funders)}",
            file=sys.stderr,
        )
    for f in funders:
        payload = _funder_to_lender_payload(f)
        action, zoho_id = _upsert_lender(client, payload)
        excl_count = len(payload.get("Excluded_Industries") or [])
        print(
            f"{action:6} {f.name:30} zoho_id={zoho_id} "
            f"min_rev={payload.get('Minimum_Monthly_Revenue')} "
            f"max_adv={payload.get('Maximum_Funding_Amount')} "
            f"excl_picklist={excl_count} "
            f"type={payload.get('Lender_Type')!r}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
