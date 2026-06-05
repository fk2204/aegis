"""Re-classify counterparties for one merchant (or every Close-sourced
merchant) using the current dictionary. READ-ONLY in this build —
prints what the classifier would produce, does NOT write to the
database.

Why this exists
---------------

The counterparty dictionary in ``aegis.counterparty.patterns`` will
grow over time as the operator finds new processors, regional bank
transfer shapes, etc. We don't want to re-parse PDFs (Bedrock cost,
parse retry windows) just to re-label transactions. This script reads
``transactions`` rows from Supabase and runs them back through the
classifier so the operator can see the effect of a dictionary edit on
real historical data BEFORE it gets persisted.

Persistence is a separate, deliberate follow-up commit (see
``docs/REMAINING_WORK.md``). This build keeps the classifier purely
additive — no scoring impact, no decline-boundary changes, no schema
migration. The script's job is to make the operator's "did this
pattern edit help?" loop fast.

Usage::

    .venv/bin/python scripts/reclassify_counterparties.py \\
        --merchant-id 5cf4479d-c6ac-4267-a2f7-5e7ef04c1345

    # Summary only (no per-row detail):
    .venv/bin/python scripts/reclassify_counterparties.py \\
        --merchant-id 5cf4479d-c6ac-4267-a2f7-5e7ef04c1345 --summary

    # All Close-sourced merchants:
    .venv/bin/python scripts/reclassify_counterparties.py --all
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from aegis.counterparty import classify_bundle
from aegis.counterparty.models import BundleSummary
from aegis.parser.models import ClassifiedTransaction

if TYPE_CHECKING:
    from supabase import Client


def _build_transactions(
    sb: Client, merchant_id: str
) -> tuple[dict[str, list[ClassifiedTransaction]], set[str], list[dict[str, Any]]]:
    """Pull transactions + analyses for one merchant, reconstruct
    ``ClassifiedTransaction`` objects, and return the bundle + the
    account-last4 set + raw doc metadata for the report."""
    docs = cast(
        list[dict[str, Any]],
        sb.table("documents")
        .select("id,original_filename,parse_status")
        .eq("merchant_id", merchant_id)
        .execute()
        .data
        or [],
    )
    by_doc: dict[str, list[ClassifiedTransaction]] = {}
    accounts: set[str] = set()
    doc_meta: list[dict[str, Any]] = []
    for d in docs:
        analyses = cast(
            list[dict[str, Any]],
            sb.table("analyses")
            .select(
                "document_id,bank_name,account_last4,"
                "statement_period_start,statement_period_end"
            )
            .eq("document_id", d["id"])
            .execute()
            .data
            or [],
        )
        if not analyses:
            continue
        a = analyses[0]
        last4_value = a.get("account_last4")
        last4 = str(last4_value) if last4_value is not None else None
        if last4:
            accounts.add(last4)
        txns_raw = cast(
            list[dict[str, Any]],
            sb.table("transactions")
            .select(
                "id,posted_date,description,amount,category,"
                "classification_confidence,running_balance,"
                "source_page,source_line"
            )
            .eq("document_id", d["id"])
            .order("posted_date")
            .execute()
            .data
            or [],
        )
        txns: list[ClassifiedTransaction] = []
        for t in txns_raw:
            running = t.get("running_balance")
            txns.append(
                ClassifiedTransaction(
                    id=UUID(t["id"]),
                    posted_date=date.fromisoformat(t["posted_date"]),
                    description=t["description"],
                    amount=Decimal(str(t["amount"])),
                    running_balance=(
                        Decimal(str(running)) if running is not None else None
                    ),
                    source_page=t["source_page"],
                    source_line=t["source_line"],
                    category=t["category"],
                    classification_confidence=t["classification_confidence"],
                )
            )
        by_doc[d["id"]] = txns
        doc_meta.append(
            {
                "document_id": d["id"],
                "filename": d.get("original_filename") or "",
                "last4": last4,
                "period_start": a.get("statement_period_start"),
                "period_end": a.get("statement_period_end"),
                "txn_count": len(txns),
            }
        )
    return by_doc, accounts, doc_meta


def _print_summary(
    label: str, summary: BundleSummary, doc_meta: list[dict[str, Any]]
) -> None:
    print()
    print("=" * 100)
    print(f"  {label}")
    print("=" * 100)
    print("  documents:")
    for d in doc_meta:
        print(
            f"    {d['document_id'][:8]}  acct=...{d['last4'] or '?':4}  "
            f"{d['period_start']} → {d['period_end']}  "
            f"{d['txn_count']:3d} txns  {d['filename'][:50]}"
        )
    print(f"\n  total transactions: {summary.transaction_count}")
    print(f"  matched own_account pairs: {summary.matched_pair_count}")
    print(f"  unconfirmed account last4s: {list(summary.unconfirmed_account_last4s)}")
    print("\n  by_class:")
    for cls in sorted(summary.by_class, key=lambda k: -summary.by_class[k]):
        print(f"    {cls:30}  {summary.by_class[cls]:>4d}")


def _print_per_row(
    by_doc: dict[str, list[ClassifiedTransaction]],
    classifications: dict[UUID, Any],
) -> None:
    print()
    print("  per-row detail (showing all non-unknown + first 10 unknown):")
    print(
        f"    {'date':10}  {'amt':>12}  {'cat':10}  "
        f"{'counterparty':24}  {'reason':30}  desc"
    )
    print("    " + "-" * 130)
    unknown_shown = 0
    reason_counter: Counter[str] = Counter()
    for txns in by_doc.values():
        for t in txns:
            cc = classifications[t.id]
            reason_counter[cc.reason] += 1
            if cc.counterparty == "unknown" and unknown_shown >= 10:
                continue
            if cc.counterparty == "unknown":
                unknown_shown += 1
            print(
                f"    {t.posted_date}  {t.amount:>12}  "
                f"{t.category:10}  {cc.counterparty:24}  "
                f"{cc.reason:30}  {t.description[:60]}"
            )
    print()
    print("  reason histogram:")
    for r, n in reason_counter.most_common():
        print(f"    {r:32}  {n:>4d}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--merchant-id", type=str)
    g.add_argument(
        "--all",
        action="store_true",
        help="Reclassify every Close-sourced merchant (close_lead_id IS NOT NULL).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Skip per-row detail. Useful when running over --all.",
    )
    args = parser.parse_args()

    from aegis.db import get_supabase

    sb = get_supabase()

    targets: list[tuple[str, str]] = []
    if args.merchant_id:
        m = cast(
            list[dict[str, Any]],
            sb.table("merchants")
            .select("id,business_name")
            .eq("id", args.merchant_id)
            .execute()
            .data
            or [],
        )
        if not m:
            print(f"merchant not found: {args.merchant_id}", file=sys.stderr)
            return 2
        targets.append((str(m[0]["id"]), str(m[0].get("business_name") or "?")))
    else:
        rows = cast(
            list[dict[str, Any]],
            sb.table("merchants")
            .select("id,business_name")
            .not_.is_("close_lead_id", "null")
            .execute()
            .data
            or [],
        )
        targets = [
            (str(r["id"]), str(r.get("business_name") or "?")) for r in rows
        ]

    for merchant_id, name in targets:
        by_doc, accounts, doc_meta = _build_transactions(sb, merchant_id)
        if not by_doc:
            print(f"\n[{name}] no transactions (parse_status != proceed?)")
            continue
        classifications, summary = classify_bundle(by_doc, accounts)
        _print_summary(f"{name} :: {merchant_id}", summary, doc_meta)
        if not args.summary:
            _print_per_row(by_doc, classifications)

    return 0


if __name__ == "__main__":
    sys.exit(main())
