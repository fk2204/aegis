"""Poll VU 7722 parse status + show analysis when ready."""

from __future__ import annotations

import sys
from typing import Any, cast

from supabase import create_client

from aegis.config import get_settings

DOC_IDS = [
    "35270827-7e12-46d9-ba4c-a3390c37b089",
    "9ea69506-694f-4599-8fa2-df4f247251b9",
    "1c4c883a-ce9e-4d85-b4dc-850e721ebcb6",
    "268d20ca-2c9c-4221-9ce5-64aaee1ee0ad",
]


def main() -> None:
    s = get_settings()
    if s.supabase_service_key is None:
        print("SUPABASE_SERVICE_KEY is not configured.", file=sys.stderr)
        return
    client = create_client(
        s.supabase_url, s.supabase_service_key.get_secret_value()
    )

    # Supabase-py returns `Any`-shaped JSON; cast to a concrete row dict shape
    # so mypy --strict can verify the dict accesses below. Justified Any:
    # row values come straight from JSON and are not statically typed by the
    # client library.
    rows = cast(
        list[dict[str, Any]],
        client.table("documents")
        .select("id,original_filename,parse_status,fraud_score")
        .in_("id", DOC_IDS)
        .execute()
        .data
        or [],
    )

    print("Document parse status:")
    print("=" * 70)
    for row in sorted(rows, key=lambda x: str(x["original_filename"])):
        status = row.get("parse_status", "")
        name = row["original_filename"]
        fs = row.get("fraud_score")
        print(f"  {status:14s}  fraud_score={fs}  {name}")

    print()
    print("Analyses:")
    print("=" * 70)
    analyses = cast(
        list[dict[str, Any]],
        client.table("analyses")
        .select(
            "document_id,statement_period_start,statement_period_end,"
            "statement_days,true_revenue,avg_daily_balance,lowest_balance,"
            "bank_name,account_last4"
        )
        .in_("document_id", DOC_IDS)
        .execute()
        .data
        or [],
    )
    if not analyses:
        print("  (no analyses yet)")
    for a in sorted(
        analyses, key=lambda x: str(x.get("statement_period_start") or "")
    ):
        print(
            f"  {a.get('statement_period_start')} -> "
            f"{a.get('statement_period_end')} "
            f"({a.get('statement_days')}d)  "
            f"bank={a.get('bank_name')!r}  "
            f"acct={a.get('account_last4')!r}"
        )
        print(
            f"    true_revenue={a.get('true_revenue')}  "
            f"adb={a.get('avg_daily_balance')}  "
            f"lowest={a.get('lowest_balance')}"
        )


if __name__ == "__main__":
    main()
