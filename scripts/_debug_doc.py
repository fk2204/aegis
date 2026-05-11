"""Dev/ops only: print a digest of one document's extraction so we can
diagnose reconciliation failures without re-running the parser.

Usage:
    /usr/local/bin/uv run python scripts/_debug_doc.py <filename_substring>
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any, cast

from aegis.db import get_supabase


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _debug_doc.py <filename_substring>", file=sys.stderr)
        return 2
    needle = sys.argv[1]
    sb = get_supabase()

    docs = cast(
        list[dict[str, Any]],
        sb.table("documents")
        .select("id, original_filename, parse_status, all_flags")
        .like("original_filename", f"%{needle}%")
        .execute()
        .data
        or [],
    )

    for d in docs:
        doc_id = d["id"]
        print(f"=== {d['original_filename']} ({d['parse_status']}) ===")
        flags = d.get("all_flags") or []
        for f in flags:
            print(f"  flag: {f}")

        txs = cast(
            list[dict[str, Any]],
            sb.table("transactions")
            .select("*")
            .eq("document_id", doc_id)
            .execute()
            .data
            or [],
        )
        analysis = cast(
            list[dict[str, Any]],
            sb.table("analyses")
            .select("*")
            .eq("document_id", doc_id)
            .execute()
            .data
            or [],
        )

        print(f"  transactions stored: {len(txs)}")
        pos = sum(Decimal(str(t["amount"])) for t in txs if Decimal(str(t["amount"])) > 0)
        neg = sum(Decimal(str(t["amount"])) for t in txs if Decimal(str(t["amount"])) < 0)
        print(f"  sum positive (deposits): {pos}")
        print(f"  sum negative (withdrawals): {neg}")

        if analysis:
            a = analysis[0]
            print(
                f"  analysis: begin={a.get('beginning_balance')} end={a.get('ending_balance')} "
                f"period={a.get('statement_period_start')} -> {a.get('statement_period_end')}"
            )

        tiny = [t for t in txs if abs(Decimal(str(t["amount"]))) < Decimal("5")]
        if tiny:
            print(f"  rows with abs(amount) < $5.00: {len(tiny)}")
            for t in tiny[:6]:
                desc = (t.get("description") or "")[:55]
                print(
                    f"    p{t['source_page']:>2}.L{t['source_line']:>3} "
                    f"amount={t['amount']:>10}  cat={t.get('category', '?'):12s}  {desc}"
                )

        # Sample a few rows across the document to eyeball
        if txs:
            print("  sample rows:")
            sample = txs[:3] + (txs[len(txs) // 2 :][:2] if len(txs) > 5 else []) + txs[-2:]
            seen_ids = set()
            for t in sample:
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                desc = (t.get("description") or "")[:55]
                print(
                    f"    p{t['source_page']:>2}.L{t['source_line']:>3} "
                    f"{t.get('posted_date', '?')} amount={t['amount']:>10} "
                    f"cat={t.get('category', '?'):12s}  {desc}"
                )

        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
