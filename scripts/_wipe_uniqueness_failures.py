"""One-shot: wipe docs whose only flag was source_uniqueness.

The recent parser fix demoted `missing_source_uniqueness` from a hard
failure to a warning. Documents that ONLY failed on that flag should
re-parse cleanly under the new policy. Documents with OTHER failures
(reconciliation_failed_*, etc.) stay put because re-parsing won't
help — they have real math issues that need operator review.

Returns the filenames that were wiped so the operator can re-upload
just those.
"""

from __future__ import annotations

import sys
from typing import Any, cast

from aegis.db import get_supabase


def main() -> int:
    sb = get_supabase()
    docs = cast(
        list[dict[str, Any]],
        sb.table("documents")
        .select("id, original_filename, parse_status, all_flags")
        .eq("parse_status", "manual_review")
        .execute()
        .data
        or [],
    )

    to_wipe: list[dict[str, Any]] = []
    for d in docs:
        flags = d.get("all_flags") or []
        # Only consider docs whose math failures are ONLY source_uniqueness.
        # Anything with reconciliation_failed_* keeps its row.
        math_flags = [f for f in flags if f.startswith("[MATH]")]
        if math_flags and all("source_uniqueness" in f for f in math_flags):
            to_wipe.append(d)

    print(f"wiping {len(to_wipe)} doc(s) whose only [MATH] flag was source_uniqueness:")
    for d in to_wipe:
        print(f"  {d['original_filename']}")

    for d in to_wipe:
        doc_id = d["id"]
        sb.table("transactions").delete().eq("document_id", doc_id).execute()
        sb.table("analyses").delete().eq("document_id", doc_id).execute()
        sb.table("documents").delete().eq("id", doc_id).execute()

    print("\nfilenames to re-upload (paste into upload-statements.ps1 filter):")
    for d in to_wipe:
        print(d["original_filename"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
