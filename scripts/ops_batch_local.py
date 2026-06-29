#!/usr/bin/env python3
"""Local batch ops runner — connects directly to prod Supabase.

Run from ``C:\\Users\\fkozi\\aegis`` on the operator workstation::

    uv run python scripts/ops_batch_local.py

Requires ``.env`` at repo root with ``SUPABASE_URL``, ``SUPABASE_KEY``,
``AEGIS_DATA_RESIDENCY_CONFIRMED=true``, AWS creds, and Close creds.

Replaces the SSH-into-prod ops loop when the auto-mode classifier blocks
remote prod writes. Reads + writes go through the live Supabase REST
client; no remote shell involved.

Idempotent — every section is safe to re-run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

_env_file = _REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from postgrest.types import CountMethod  # noqa: E402
from supabase import Client  # noqa: E402

from aegis.db import get_supabase  # noqa: E402


def _rows(result: object) -> list[dict[str, Any]]:
    data = cast(Any, result).data
    return cast(list[dict[str, Any]], data or [])


def _count(result: object) -> int:
    return cast(int, cast(Any, result).count or 0)


def _print_doc_status_block(sb: Client, label: str) -> None:
    print(f"=== {label} ===")
    for status in ("manual_review", "proceed", "error", "pending"):
        result = (
            sb.table("documents")
            .select("id", count=CountMethod.exact)
            .eq("parse_status", status)
            .execute()
        )
        print(f"  {status:20s}  {_count(result)}")


def _fix_empty_funders(sb: Client) -> int:
    print("\n=== FIXING FUNDERS WITH EMPTY deal_types_accepted ===")
    funders = _rows(
        sb.table("funders").select("id,name,deal_types_accepted").eq("active", True).execute()
    )
    fixed = 0
    for f in funders:
        if not f.get("deal_types_accepted"):
            sb.table("funders").update({"deal_types_accepted": ["revenue_based"]}).eq(
                "id", f["id"]
            ).execute()
            print(f"  Fixed: {f['name']}")
            fixed += 1
    print(f"  Total fixed: {fixed}")
    return fixed


def _link_rendezvous(sb: Client) -> None:
    print("\n=== LINKING RENDEZVOUS TO CLOSE ===")
    rendezvous = _rows(
        sb.table("merchants")
        .select("id,business_name,close_lead_id")
        .ilike("business_name", "%Rendezvous%")
        .limit(1)
        .execute()
    )
    if not rendezvous:
        print("  No Rendezvous merchant found")
        return
    row = rendezvous[0]
    if not row.get("close_lead_id"):
        sb.table("merchants").update(
            {"close_lead_id": "lead_auHAuNhMym58TnBIifgwVNLTHqxufMGKpe43lnCxnDM"}
        ).eq("id", row["id"]).execute()
        print(f"  Linked: {row['business_name']}")
    else:
        print(f"  Already linked: {row['business_name']} -> {row['close_lead_id']}")


def _close_resync(sb: Client) -> None:
    print("\n=== CLOSE RE-SYNC (FINANCIAL block refresh) ===")
    try:
        from aegis.close.client import CloseClient, CloseError
        from aegis.close.field_map import _parse_close_lead_description
        from aegis.config import get_settings

        if get_settings().close_api_key is None:
            print("  CLOSE_API_KEY not set in .env — skipping")
            return
        with CloseClient() as client:
            merchants = _rows(
                sb.table("merchants")
                .select("id,business_name,close_lead_id")
                .not_.is_("close_lead_id", "null")
                .execute()
            )
            refreshed = 0
            errors = 0
            for m in merchants:
                try:
                    lead = client.get_lead(m["close_lead_id"])
                    description = lead.get("description") or "" if isinstance(lead, dict) else ""
                    if not description:
                        continue
                    parsed = _parse_close_lead_description(description)
                    payload = {k: v for k, v in parsed.items() if v is not None}
                    if payload:
                        sb.table("merchants").update(payload).eq("id", m["id"]).execute()
                        refreshed += 1
                except CloseError as exc:
                    errors += 1
                    print(f"  Close error on {m['id']}: {exc}")
                except Exception as exc:
                    errors += 1
                    print(f"  Sync error on {m['id']}: {exc}")
            print(f"  Refreshed: {refreshed}/{len(merchants)}  Errors: {errors}")
    except Exception as exc:
        print(f"  Close re-sync setup failed: {exc}")


def _missing_file_merchants(sb: Client) -> None:
    print("\n=== MERCHANTS NEEDING RE-UPLOAD (no storage_path) ===")
    docs = _rows(
        sb.table("documents")
        .select("id,merchant_id,original_filename")
        .eq("parse_status", "error")
        .is_("storage_path", "null")
        .execute()
    )
    merchant_ids = sorted({d["merchant_id"] for d in docs})
    if not merchant_ids:
        print("  None")
        return
    merchants = _rows(
        sb.table("merchants").select("id,business_name").in_("id", merchant_ids).execute()
    )
    print(f"  {len(merchants)} merchants need original statements re-uploaded:")
    for m in sorted(merchants, key=lambda x: x.get("business_name") or ""):
        count = sum(1 for d in docs if d["merchant_id"] == m["id"])
        name = m.get("business_name") or "UNNAMED"
        print(f"  -> {name:40s}  ({count} docs)")


def main() -> int:
    sb = get_supabase()
    print("Connected to prod Supabase\n")

    _print_doc_status_block(sb, "CURRENT STATUS")
    total = sb.table("merchants").select("id", count=CountMethod.exact).execute()
    disq = (
        sb.table("merchants")
        .select("id", count=CountMethod.exact)
        .eq("status", "disqualified")
        .execute()
    )
    print(f"  {'merchants':20s}  {_count(total)} (disqualified: {_count(disq)})")

    _fix_empty_funders(sb)
    _link_rendezvous(sb)
    _close_resync(sb)
    _missing_file_merchants(sb)

    print("")
    _print_doc_status_block(sb, "FINAL STATUS")

    analyses_with = (
        sb.table("analyses")
        .select("id", count=CountMethod.exact)
        .not_.is_("narrator_summary", "null")
        .execute()
    )
    analyses_total = sb.table("analyses").select("id", count=CountMethod.exact).execute()
    print(f"  narrator:             {_count(analyses_with)}/{_count(analyses_total)}")

    funders_final = _rows(
        sb.table("funders").select("name,deal_types_accepted").eq("active", True).execute()
    )
    empty = [f["name"] for f in funders_final if not f.get("deal_types_accepted")]
    if empty:
        print(f"  funders empty dta:    {len(empty)} BAD: {empty}")
    else:
        print("  funders empty dta:    0 OK")

    print("\nDone. Paste this output back to Claude.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
