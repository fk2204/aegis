"""Tiny probe — reads parse_status for one document_id and prints it.

Used by scripts/canary_upload.ps1 to avoid embedding python-in-bash-in-
powershell. Runs on the prod box as the aegis user via:

    /usr/local/bin/uv run python scripts/_status_probe.py <document_id>

Prints exactly one line — the parse_status (or "missing") — so callers
can grep / match the result without parsing JSON.
"""

from __future__ import annotations

import sys
from typing import Any, cast

from aegis.db import get_supabase


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _status_probe.py <document_id>", file=sys.stderr)
        return 2
    doc_id = sys.argv[1]
    sb = get_supabase()
    r = sb.table("documents").select("parse_status").eq("id", doc_id).execute()
    if not r.data:
        print("missing")
        return 1
    row = cast(dict[str, Any], r.data[0])
    print(row["parse_status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
