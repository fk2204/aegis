"""Wipe a single document by id so the same PDF can be re-uploaded
and re-parsed cleanly.

Narrow-scope sibling of ``scripts/_reparse_wipe.py``. Use when you
want to verify a write-path change (e.g. stage 2 chunk 2's
``pattern_analysis`` population) on ONE document, not the entire
documents table.

Delete order matches ``_reparse_wipe.py`` so FK constraints are
respected:
    transactions -> analyses -> documents
Merchants + audit_log are KEPT — the merchant row stays attached to
its merchant_id history; the audit feed preserves the parse + upload
trail of the original.

Re-parse flow after wipe:
    1. This script deletes the document's child rows + the document row.
    2. Operator re-uploads the same PDF via /ui/upload.
    3. The upload's SHA256 dedup sees no existing row (we deleted it)
       and persists a fresh document + enqueues a parse_document job
       on arq.
    4. Worker parses, persists the new analyses row (with populated
       pattern_analysis from chunk 2 onward).

Per CLAUDE.md operating-principles §1 (production data writes require
explicit operator approval per action) the script prompts for
confirmation interactively before any DELETE runs. ``YES`` typed
literally to proceed; anything else aborts with exit code 1.

Usage:
    uv run python scripts/_reparse_one.py --document-id <UUID>
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, cast
from uuid import UUID

from aegis.db import get_supabase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--document-id",
        required=True,
        help="UUID of the document to wipe. Operator re-uploads the same "
        "PDF after wipe to trigger a fresh parse.",
    )
    args = parser.parse_args()

    try:
        doc_uuid = UUID(args.document_id)
    except ValueError:
        print(
            f"ERROR: --document-id is not a valid UUID: {args.document_id}",
            file=sys.stderr,
        )
        return 2

    sb = get_supabase()
    rows = cast(
        list[dict[str, Any]],
        sb.table("documents")
        .select("id, original_filename, parse_status, parsed_at")
        .eq("id", str(doc_uuid))
        .execute()
        .data
        or [],
    )

    if not rows:
        print(
            f"ERROR: no document with id {doc_uuid}",
            file=sys.stderr,
        )
        return 2

    doc = rows[0]
    print("Document to wipe:")
    print(f"  id:              {doc['id']}")
    print(f"  filename:        {doc.get('original_filename') or '(unknown)'}")
    print(f"  parse_status:    {doc.get('parse_status', '?')}")
    print(f"  parsed_at:       {doc.get('parsed_at') or '(never)'}")
    print()
    print(
        "This deletes the document's transactions, analyses row, and the "
        "documents row itself."
    )
    print(
        "The same PDF can then be re-uploaded via /ui/upload to trigger a "
        "fresh parse."
    )
    print("Merchants + audit_log are KEPT.")
    print()

    try:
        reply = input("Type YES to proceed: ").strip()
    except EOFError:
        # Non-interactive invocation (pipe, no TTY). Refuse to proceed —
        # the confirmation gate exists precisely to require operator
        # presence at apply time.
        print("ERROR: stdin closed; aborting.", file=sys.stderr)
        return 1

    if reply != "YES":
        print("Aborted.")
        return 1

    sb.table("transactions").delete().eq("document_id", str(doc_uuid)).execute()
    sb.table("analyses").delete().eq("document_id", str(doc_uuid)).execute()
    sb.table("documents").delete().eq("id", str(doc_uuid)).execute()

    print(
        f"Done — wiped document {doc_uuid} "
        f"({doc.get('original_filename') or 'unnamed'})."
    )
    print(
        "Now re-upload the same PDF via /ui/upload to trigger a fresh parse."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
