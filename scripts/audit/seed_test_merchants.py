"""Audit-time helper: seed placeholder merchants + link orphan documents.

ONLY for dev/test environments. Creates 4 merchants with hardcoded
placeholder owner names + bank-product business names, then links any
orphan parsed documents to them by filename prefix. Useful for
exercising the dashboard against a known corpus.

NEVER run against production Supabase. An earlier session did exactly
that without explicit operator authorization and the placeholders
shipped to staff, who reasonably concluded the parser was hallucinating
(it wasn't — the merchant rows were stubs). Multiple barriers now:

  1. --confirm flag required (else dry run + exit).
  2. --env flag required, one of {dev, production}.
  3. If --env=production, AEGIS_ALLOW_PRODUCTION_SEED=true env var
     also required. This var is intentionally NOT set in
     /etc/aegis/aegis.env or any committed file — operator must
     export it ad-hoc to override.

Usage:

    # Dry run — prints what would happen, no writes.
    uv run python scripts/audit/seed_test_merchants.py --env=dev

    # Actually seed dev:
    uv run python scripts/audit/seed_test_merchants.py --env=dev --confirm

    # Seed production (requires explicit override env var):
    AEGIS_ALLOW_PRODUCTION_SEED=true uv run python \\
        scripts/audit/seed_test_merchants.py --env=production --confirm
"""

from __future__ import annotations

import argparse
import os
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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed placeholder merchants for audit/testing. "
        "Refuses to write without --confirm. Refuses production writes "
        "without AEGIS_ALLOW_PRODUCTION_SEED=true."
    )
    p.add_argument(
        "--env",
        required=True,
        choices=("dev", "production"),
        help="Target environment. 'production' requires "
        "AEGIS_ALLOW_PRODUCTION_SEED=true env var.",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Actually perform the writes. Without this flag the script "
        "runs in dry-run mode (lists what would happen, exits 0).",
    )
    return p.parse_args()


def _gate(env: str, confirm: bool) -> bool:
    """Return True if writes should proceed, False if dry-run only."""
    if not confirm:
        print("DRY RUN (no --confirm flag). Would perform the following writes:\n")
        return False
    if env == "production":
        if os.environ.get("AEGIS_ALLOW_PRODUCTION_SEED", "").lower() != "true":
            print(
                "REFUSED: --env=production requires "
                "AEGIS_ALLOW_PRODUCTION_SEED=true in the environment.\n"
                "  This is a deliberate barrier — placeholder merchant rows "
                "must never reach production again.",
                file=sys.stderr,
            )
            sys.exit(2)
    return True


def main() -> int:
    args = _parse_args()
    will_write = _gate(args.env, args.confirm)
    sb = get_supabase()

    docs = cast(
        list[dict[str, Any]],
        sb.table("documents")
        .select("id, original_filename, merchant_id")
        .execute()
        .data
        or [],
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
            if will_write:
                r = sb.table("merchants").insert(payload).execute()
                rows = cast(list[dict[str, Any]], r.data or [])
                merchant_id = rows[0]["id"]
                print(f"  created merchant: {name} ({merchant_id})")
                by_name[name] = merchant_id
            else:
                print(f"  WOULD CREATE merchant: {name}")
                merchant_id = "(dry-run)"
                by_name[name] = merchant_id

        # Link any orphan doc whose filename starts with this prefix
        linked = 0
        for d in docs:
            fn = d.get("original_filename") or ""
            if d.get("merchant_id") is None and fn.startswith(prefix):
                if will_write:
                    sb.table("documents").update({"merchant_id": merchant_id}).eq(
                        "id", d["id"]
                    ).execute()
                    sb.table("analyses").update({"merchant_id": merchant_id}).eq(
                        "document_id", d["id"]
                    ).execute()
                linked += 1
        verb = "linked" if will_write else "WOULD LINK"
        print(f"    {verb} {linked} documents")

    if not will_write:
        print(
            "\nDry run complete. Re-run with --confirm to apply.\n"
            "  For --env=production also set AEGIS_ALLOW_PRODUCTION_SEED=true."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
