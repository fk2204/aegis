# ruff: noqa: E501
# Operator diagnostic script: readable single-line print output of reconciliation
# rows is more useful at the terminal than wrapped lines. E501 suppressed file-wide.
"""Cross-account reconciliation between VU's CHK 7719 and CHK 7722.

VU Development gave us their second-account (7722) statements after we
had already parsed the first-account (7719) statements. The 7719
statements had a pattern of large "Online Banking transfer to CHK 7722"
debits with no matching credit in the bundle — at the time the parser's
unreconciled_internal_transfer detector flagged these as a hidden-
account signal. With the 7722 statements now in, the corresponding
"transfer from CHK 7719" credits SHOULD appear and reconcile.

This script:
1. Lists per-account analyses (revenue, ADB, lowest balance).
2. Compares the per-account periods (do the months line up?).
3. Pulls every transfer transaction from both accounts, attempts to
   match outflows on one side to inflows on the other by magnitude +
   date proximity, and reports the reconciliation rate.

Read-only.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from typing import Any, cast

from supabase import Client, create_client

from aegis.config import get_settings

MERCHANT_ID = "5cf4479d-c6ac-4267-a2f7-5e7ef04c1345"
ACCT_7719_DOC_IDS = [
    "49c7d058-3e2a-4554-ad46-f4063146b36e",  # Feb
    "932dca55-2fed-4ea6-93e4-b0cb413a57cd",  # Mar
    "f4cb4d34-f1ec-48db-aa5f-876a08ef242c",  # Apr
    "30e6d5f7-cbc2-4282-8350-198eac75cd75",  # May
]
ACCT_7722_DOC_IDS = [
    "35270827-7e12-46d9-ba4c-a3390c37b089",  # Feb
    "9ea69506-694f-4599-8fa2-df4f247251b9",  # Mar
    "1c4c883a-ce9e-4d85-b4dc-850e721ebcb6",  # Apr
    "268d20ca-2c9c-4221-9ce5-64aaee1ee0ad",  # May
]


def _client() -> Client:
    s = get_settings()
    if s.supabase_service_key is None:
        print("SUPABASE_SERVICE_KEY is not configured.", file=sys.stderr)
        sys.exit(2)
    return create_client(
        s.supabase_url, s.supabase_service_key.get_secret_value()
    )


def main() -> None:
    sb = _client()

    # 1. Per-account analyses
    print("PER-ACCOUNT ANALYSES")
    print("=" * 100)
    for label, ids in (("7719", ACCT_7719_DOC_IDS), ("7722", ACCT_7722_DOC_IDS)):
        print(f"\n--- account {label} ---")
        # cast Any-shaped JSON rows from supabase-py to a concrete dict shape
        # so mypy --strict can verify the field accesses below.
        anals = cast(
            list[dict[str, Any]],
            sb.table("analyses")
            .select(
                "document_id,statement_period_start,statement_period_end,"
                "statement_days,true_revenue,avg_daily_balance,lowest_balance,"
                "num_nsf,days_negative,bank_name,account_last4"
            )
            .in_("document_id", ids)
            .execute()
            .data
            or [],
        )
        for a in sorted(
            anals, key=lambda x: str(x.get("statement_period_start") or "")
        ):
            print(
                f"  {a['statement_period_start']} -> {a['statement_period_end']}  "
                f"days={a['statement_days']:>3}  "
                f"bank={a['bank_name']!r:30s}  acct={a['account_last4']!r}"
            )
            print(
                f"    true_revenue=${a['true_revenue']:>14}  "
                f"adb=${a['avg_daily_balance']:>14}  "
                f"lowest=${a['lowest_balance']:>14}  "
                f"nsf={a['num_nsf']}  neg_days={a['days_negative']}"
            )

    # 2. Pull all transfers from both sets
    all_ids = ACCT_7719_DOC_IDS + ACCT_7722_DOC_IDS
    txns = cast(
        list[dict[str, Any]],
        sb.table("transactions")
        .select(
            "document_id,id,posted_date,amount,description,category"
        )
        .in_("document_id", all_ids)
        .eq("category", "transfer")
        .execute()
        .data
        or [],
    )

    # 3. Split by account + direction
    in_7719 = ACCT_7719_DOC_IDS
    in_7722 = ACCT_7722_DOC_IDS
    out_7719: list[dict[str, Any]] = []   # debits on 7719 (transfer to 7722)
    in_to_7722: list[dict[str, Any]] = []  # credits on 7722 (transfer from 7719)
    out_7722: list[dict[str, Any]] = []   # debits on 7722 (transfer to 7719)
    in_to_7719: list[dict[str, Any]] = []  # credits on 7719 (transfer from 7722)
    for t in txns:
        amt = Decimal(str(t["amount"]))
        if t["document_id"] in in_7719:
            if amt < 0:
                out_7719.append(t)
            else:
                in_to_7719.append(t)
        elif t["document_id"] in in_7722:
            if amt < 0:
                out_7722.append(t)
            else:
                in_to_7722.append(t)

    print("\n\nTRANSFER STREAM SUMMARY (transactions classified `transfer`)")
    print("=" * 100)
    print(f"  7719 OUT (to 7722-ish):  {len(out_7719)} legs, sum=${sum(abs(Decimal(str(t['amount']))) for t in out_7719):,.2f}")
    print(f"  7722 IN  (from 7719-ish): {len(in_to_7722)} legs, sum=${sum(Decimal(str(t['amount'])) for t in in_to_7722):,.2f}")
    print(f"  7722 OUT (to 7719-ish):  {len(out_7722)} legs, sum=${sum(abs(Decimal(str(t['amount']))) for t in out_7722):,.2f}")
    print(f"  7719 IN  (from 7722-ish): {len(in_to_7719)} legs, sum=${sum(Decimal(str(t['amount'])) for t in in_to_7719):,.2f}")

    # 4. Pairing: for each 7719 OUT, look for a 7722 IN within +- 3 days
    #    with magnitude within $1.
    def _try_pair(
        outs: list[dict[str, Any]],
        ins: list[dict[str, Any]],
        label: str,
    ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
        from datetime import datetime
        print(f"\n\n{label}")
        print("-" * 100)
        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        unmatched: list[dict[str, Any]] = []
        used: set[int] = set()
        for o in outs:
            o_amt = abs(Decimal(str(o["amount"])))
            o_date = datetime.fromisoformat(o["posted_date"]).date() if "T" in o["posted_date"] else date.fromisoformat(o["posted_date"])
            match = None
            for i_idx, i in enumerate(ins):
                if i_idx in used:
                    continue
                i_amt = Decimal(str(i["amount"]))
                i_date = datetime.fromisoformat(i["posted_date"]).date() if "T" in i["posted_date"] else date.fromisoformat(i["posted_date"])
                if abs(i_amt - o_amt) < Decimal("1.00") and abs((i_date - o_date).days) <= 3:
                    match = (i_idx, i)
                    break
            if match:
                used.add(match[0])
                matched.append((o, match[1]))
            else:
                unmatched.append(o)
        print(f"  matched:   {len(matched)} of {len(outs)}")
        print(f"  unmatched: {len(unmatched)}")
        if matched:
            sample = matched[:5]
            print("  sample matches:")
            for o, i in sample:
                print(f"    {o['posted_date'][:10]}  -${abs(Decimal(str(o['amount']))):>12,.2f}  ->  {i['posted_date'][:10]}  +${Decimal(str(i['amount'])):>12,.2f}")
        if unmatched:
            print("  sample unmatched debits:")
            for o in unmatched[:5]:
                print(f"    {o['posted_date'][:10]}  -${abs(Decimal(str(o['amount']))):>12,.2f}  {o['description'][:80]}")
        return matched, unmatched

    _try_pair(out_7719, in_to_7722, "PAIRING: 7719 OUT -> 7722 IN")
    _try_pair(out_7722, in_to_7719, "PAIRING: 7722 OUT -> 7719 IN")


if __name__ == "__main__":
    main()
