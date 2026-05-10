"""Destructive helper for scripts/reparse_all.ps1.

Deletes every row from `transactions`, `analyses`, and `documents` so a
re-upload triggers a fresh parse. Merchants and audit_log are KEPT.

Only intended to be invoked from `reparse_all.ps1`, which already shows
the user a YES/no prompt before running this. Do not run by hand on prod
without authorization.

Order of deletes matters because of FK constraints:
  transactions -> analyses -> documents
(child tables first, then parent)
"""

from __future__ import annotations

import sys
from typing import Any, cast

from aegis.db import get_supabase


def main() -> int:
    sb = get_supabase()

    # Inventory first so the operator sees what's being wiped.
    docs = cast(
        list[dict[str, Any]],
        sb.table("documents")
        .select("id, original_filename, parse_status")
        .execute()
        .data
        or [],
    )
    print(f"  documents to wipe: {len(docs)}")
    for d in docs:
        print(
            f"    {d.get('parse_status', '?'):14s}  "
            f"{(d.get('original_filename') or '?')[:60]}"
        )

    if not docs:
        print("  nothing to do.")
        return 0

    # Delete children first, then documents themselves.
    for d in docs:
        doc_id = d["id"]
        sb.table("transactions").delete().eq("document_id", doc_id).execute()
        sb.table("analyses").delete().eq("document_id", doc_id).execute()
        sb.table("documents").delete().eq("id", doc_id).execute()

    print(f"  done — wiped {len(docs)} document(s) + their tx/analysis rows.")
    print("  merchants + audit_log retained.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
