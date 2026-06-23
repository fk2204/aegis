"""Audit-time helper: create the 3 real merchants from the corpus PDFs
and link their orphan documents.

The 14 uploaded PDFs come from 3 distinct businesses:
  * Know Your Collectibles Inc. — 6 statements (Nov 2025 - Apr 2026)
  * "Business Checking Plus x2414" — 4 statements (Chase, account x2414)
  * PNC + Wells Fargo eStmt — different account, scoping TBD by operator

This is a one-shot data-fix run only after the audit confirmed the docs
are parsed but orphan. Not for routine use. Idempotent.
"""

from __future__ import annotations

import sys
from typing import Any, cast

from aegis.db import get_supabase

_MERCHANTS = [
    {
        "business_name": "Know Your Collectibles Inc.",
        "owner_name": "(operator to fill)",
        "state": "CA",  # Placeholder — operator should edit on intake form
        "entity_type": "corp",
        "broker_source": "operator imported",
        "intake_date": "2026-05-10",
        "filename_prefix": "know-your-collectibles-inc",
    },
    {
        "business_name": "Business Checking Plus (x2414)",
        "owner_name": "(operator to fill)",
        "state": "CA",
        "entity_type": "llc",
        "broker_source": "operator imported",
        "intake_date": "2026-05-10",
        "filename_prefix": "Business_Checking_Plus_x2414",
    },
    {
        "business_name": "PNC Statement Merchant",
        "owner_name": "(operator to fill)",
        "state": "CA",
        "entity_type": "llc",
        "broker_source": "operator imported",
        "intake_date": "2026-05-10",
        "filename_prefix": "2025-May-PNC-Statement",
    },
    {
        "business_name": "Wells Fargo eStmt Merchant",
        "owner_name": "(operator to fill)",
        "state": "CA",
        "entity_type": "llc",
        "broker_source": "operator imported",
        "intake_date": "2026-05-10",
        "filename_prefix": "eStmt_",
    },
]


def main() -> int:
    sb = get_supabase()

    docs = cast(
        list[dict[str, Any]],
        sb.table("documents").select("id, original_filename, merchant_id").execute().data or [],
    )
    print(f"  {len(docs)} documents in DB; linking by filename prefix...")

    existing_merchants = cast(
        list[dict[str, Any]],
        sb.table("merchants").select("id, business_name").execute().data or [],
    )
    by_name = {m["business_name"]: m["id"] for m in existing_merchants}

    for m_def in _MERCHANTS:
        name = m_def["business_name"]
        prefix = m_def["filename_prefix"]

        if name in by_name:
            merchant_id = by_name[name]
            print(f"  reusing merchant: {name} ({merchant_id})")
        else:
            payload = {k: v for k, v in m_def.items() if k != "filename_prefix"}
            r = sb.table("merchants").insert(cast(Any, payload)).execute()
            rows = cast(list[dict[str, Any]], r.data)
            merchant_id = rows[0]["id"]
            print(f"  created merchant: {name} ({merchant_id})")
            by_name[name] = merchant_id

        # Link any orphan doc whose filename starts with this prefix
        linked = 0
        for d in docs:
            fn = d.get("original_filename") or ""
            if d.get("merchant_id") is None and fn.startswith(prefix):
                sb.table("documents").update({"merchant_id": merchant_id}).eq(
                    "id", d["id"]
                ).execute()
                # Also link the analysis row
                sb.table("analyses").update({"merchant_id": merchant_id}).eq(
                    "document_id", d["id"]
                ).execute()
                linked += 1
        print(f"    linked {linked} documents")

    return 0


if __name__ == "__main__":
    sys.exit(main())
