"""Selective Close lead import for AEGIS (P4, 2026-07-01).

Close has ~9,000 leads. AEGIS only cares about two subsets:

  1. **Qualified - Opp Open** — active underwriting deals. Anything
     not already linked to an AEGIS merchant gets a placeholder
     merchant row so the operator can attach statements.

  2. **Disqualified** — leads Commera walked away from. If a merchant
     is already in AEGIS for that Close lead AND no funder outcome
     is recorded yet, we write a ``funder_replies`` row with
     ``outcome='declined'`` so the calibration engine has ground
     truth for the decline path.

Every other Close status label (Nurture, New, Attempting, Engaged,
Cold, etc.) is ignored — those are pre-underwriting states with no
useful AEGIS signal yet.

Safety:
  * Idempotent: existing Close-linked merchants are skipped in
    group 1; merchants that already have a ``funder_replies`` row
    are skipped in group 2.
  * All secrets read from env (``CLOSE_API_KEY``, plus the standard
    Supabase creds via ``get_supabase``). Nothing prints tokens.
  * Never modifies leads already in Close — read-only against Close.

Run on the prod box via:

    systemd-run --uid=aegis --pipe --wait \\
      --property=EnvironmentFile=/etc/aegis/aegis.env \\
      /opt/aegis/.venv/bin/python \\
      /opt/aegis/scripts/bulk_import_close_leads.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any, cast

from aegis.close.client import CloseClient
from aegis.close.field_map import _parse_close_lead_description
from aegis.config import get_settings
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)

_IMPORT_STATUS = "Qualified – Opp Open"  # noqa: RUF001 en dash matches Close label
_DISQUAL_STATUS = "Disqualified"

# Close paginates via _skip / _limit. 100 is the API-approved max page.
_PAGE = 100


def _search_leads_by_status(client: CloseClient, status: str) -> list[dict[str, Any]]:
    """Page through Close for every lead matching ``lead_status_label``.

    Uses the ``GET /api/v1/lead/`` endpoint with the ``query`` param
    (Close's smart-view search syntax). The alternative POST
    ``/lead/search/`` requires a saved-search object.
    """
    results: list[dict[str, Any]] = []
    skip = 0
    while True:
        try:
            resp = client.request(
                "GET",
                "/api/v1/lead/",
                params={
                    "query": f'lead_status_label:"{status}"',
                    "_skip": skip,
                    "_limit": _PAGE,
                },
            )
        except Exception as exc:
            _log.error(
                "bulk_import.search_failed status=%s skip=%d exc=%s",
                status,
                skip,
                exc,
            )
            break
        batch = cast(list[dict[str, Any]], resp.get("data") or [])
        if not batch:
            break
        results.extend(batch)
        if not resp.get("has_more"):
            break
        skip += len(batch)
    return results


def _import_qualified(
    client: CloseClient,
    existing_by_lead: dict[str, str],
) -> tuple[int, int, int, int]:
    """Group 1 — create merchant rows for Qualified - Opp Open leads."""
    print(f"\nFetching Close leads with status: {_IMPORT_STATUS}")
    qualified = _search_leads_by_status(client, _IMPORT_STATUS)
    print(f"Found: {len(qualified)}")

    imported = 0
    skipped_exists = 0
    skipped_no_name = 0
    errors = 0

    sb = get_supabase()
    for lead in qualified:
        lead_id = lead.get("id")
        if not isinstance(lead_id, str) or not lead_id:
            continue
        if lead_id in existing_by_lead:
            skipped_exists += 1
            continue

        raw_name = lead.get("display_name") or lead.get("name") or ""
        name = str(raw_name).strip()
        if not name:
            skipped_no_name += 1
            continue

        desc = str(lead.get("description") or "")
        try:
            parsed = _parse_close_lead_description(desc)
        except Exception:
            parsed = {}

        merchant_data: dict[str, Any] = {
            "business_name": name,
            "close_lead_id": lead_id,
            "status": "provisional",
        }
        for key, value in (parsed or {}).items():
            if value is not None:
                merchant_data[key] = value

        try:
            sb.table("merchants").insert(cast(Any, merchant_data)).execute()
            imported += 1
            _log.info(
                "bulk_import.created lead_id=%s name_len=%d",
                lead_id,
                len(name),
            )
        except Exception as exc:
            errors += 1
            _log.error("bulk_import.error lead_id=%s exc=%s", lead_id, exc)

    return imported, skipped_exists, skipped_no_name, errors


def _record_disqualified_outcomes(
    client: CloseClient,
    existing_by_lead: dict[str, str],
) -> tuple[int, int]:
    """Group 2 — write funder_replies.outcome='declined' for AEGIS
    merchants whose Close lead is currently in the Disqualified bucket
    and don't yet have a recorded funder reply."""
    print(f"\nFetching Close leads with status: {_DISQUAL_STATUS}")
    disq = _search_leads_by_status(client, _DISQUAL_STATUS)
    print(f"Found: {len(disq)}")

    outcomes_recorded = 0
    outcomes_skipped = 0
    sb = get_supabase()

    for lead in disq:
        lead_id = lead.get("id")
        if not isinstance(lead_id, str) or lead_id not in existing_by_lead:
            continue

        merchant_id = existing_by_lead[lead_id]

        try:
            existing = (
                sb.table("funder_replies")
                .select("id")
                .eq("merchant_id", merchant_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            _log.warning(
                "bulk_import.reply_lookup_failed merchant=%s exc=%s",
                merchant_id,
                exc,
            )
            continue
        if existing.data:
            outcomes_skipped += 1
            continue

        payload: dict[str, Any] = {
            "merchant_id": merchant_id,
            "outcome": "declined",
            "notes": (
                "Auto-recorded: lead status is Disqualified in Close as of "
                f"{datetime.now(UTC).date().isoformat()}."
            ),
            "recorded_at": datetime.now(UTC).isoformat(),
            "source": "close_disqualified_sync",
        }
        try:
            sb.table("funder_replies").insert(cast(Any, payload)).execute()
            outcomes_recorded += 1
            _log.info(
                "bulk_import.outcome_declined merchant=%s lead_id=%s",
                merchant_id,
                lead_id,
            )
        except Exception as exc:
            _log.error(
                "bulk_import.outcome_write_failed merchant=%s exc=%s",
                merchant_id,
                exc,
            )

    return outcomes_recorded, outcomes_skipped


def main() -> int:
    settings = get_settings()
    if not getattr(settings, "close_api_key", None):
        print("CLOSE_API_KEY not configured; nothing to do.")
        return 0

    client = CloseClient()
    sb = get_supabase()

    try:
        existing_resp = (
            sb.table("merchants")
            .select("id,close_lead_id")
            .not_.is_("close_lead_id", "null")
            .execute()
        )
    except Exception as exc:
        _log.error("bulk_import.merchant_scan_failed exc=%s", exc)
        return 1

    existing_by_lead: dict[str, str] = {}
    for raw_row in existing_resp.data or []:
        if not isinstance(raw_row, dict):
            continue
        row = cast(dict[str, Any], raw_row)
        lead_id_val = row.get("close_lead_id")
        mid_val = row.get("id")
        if isinstance(lead_id_val, str) and isinstance(mid_val, str):
            existing_by_lead[lead_id_val] = mid_val
    print(f"Existing Close-linked merchants: {len(existing_by_lead)}")

    imported, sk_exists, sk_noname, errors = _import_qualified(client, existing_by_lead)
    print(
        f"Imported: {imported} | Skipped existing: {sk_exists} | "
        f"No name: {sk_noname} | Errors: {errors}"
    )

    recorded, already = _record_disqualified_outcomes(client, existing_by_lead)
    print(f"Outcomes recorded: {recorded} | Already had outcome: {already}")

    print("\nSummary:")
    print(f"  Qualified leads imported: {imported}")
    print(f"  Declined outcomes recorded: {recorded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
