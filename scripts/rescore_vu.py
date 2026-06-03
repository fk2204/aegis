"""Re-aggregate analyses for one merchant after the H1 _true_revenue fix.

Reads each document's existing classified transactions from `transactions`,
re-runs ``aggregate()`` from ``aegis.parser.aggregate``, computes the
fields that depend on ``true_revenue`` (monthly_revenue, debt_to_revenue,
true_revenue_source_ids), prints a per-doc before/after diff, **pauses
for an explicit YES**, and only then UPDATEs the analyses rows.

Per the operator's rule (memory: feedback-rewrite-prod-show-diff-first):
re-aggregation writes show the diff first, hard pause for `YES`, abort
on anything else.

Usage on prod box:

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    uv run python scripts/rescore_vu.py <merchant_id> <actor_email>
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from supabase import create_client

from aegis.api.deps import get_audit, get_repository
from aegis.config import get_settings
from aegis.parser.aggregate import aggregate
from aegis.parser.models import ClassifiedTransaction


@dataclass
class _RescoreEntry:
    document_id: UUID
    filename: str
    statement_days: int
    before_true_revenue: Decimal
    after_true_revenue: Decimal
    before_monthly_revenue: Decimal
    after_monthly_revenue: Decimal
    before_debt_to_revenue: Decimal
    after_debt_to_revenue: Decimal
    before_source_count: int
    after_source_count: int
    new_source_ids: list[str]


def _project_monthly(period_revenue: Decimal, statement_days: int) -> Decimal:
    if statement_days <= 0:
        return Decimal("0.00")
    daily = period_revenue / Decimal(statement_days)
    return (daily * Decimal(30)).quantize(Decimal("0.01"))


def _fmt_money(d: Decimal | None) -> str:
    if d is None:
        return "None"
    return f"${d:>14,.2f}"


def main(merchant_id_str: str, actor_email: str) -> int:
    merchant_id = UUID(merchant_id_str)
    settings = get_settings()
    if settings.supabase_service_key is None:
        print("SUPABASE_SERVICE_KEY is not configured.", file=sys.stderr)
        return 2
    sb = create_client(
        settings.supabase_url, settings.supabase_service_key.get_secret_value()
    )
    repo = get_repository()
    audit = get_audit()

    docs = repo.list_documents(merchant_id=merchant_id, limit=50)
    if not docs:
        print(f"No documents for merchant {merchant_id}.")
        return 1

    print(f"Merchant: {merchant_id}")
    print(f"Documents found: {len(docs)}")
    print()

    plan: list[_RescoreEntry] = []
    for d in sorted(docs, key=lambda x: x.original_filename):
        current = repo.get_analysis(d.id)
        if current is None:
            print(f"  [skip] {d.original_filename}: no analysis row")
            continue

        txns: list[ClassifiedTransaction] = repo.list_transactions(d.id)
        if not txns:
            print(f"  [skip] {d.original_filename}: no transactions")
            continue

        new_agg = aggregate(
            txns,
            period_start=current.statement_period_start,
            period_end=current.statement_period_end,
            beginning_balance=current.beginning_balance,
        ).aggregates

        new_true = new_agg.true_revenue.value
        new_monthly = _project_monthly(new_true, current.statement_days)
        new_debt_to_rev = new_agg.debt_to_revenue
        new_source_ids = [str(u) for u in new_agg.true_revenue.source_ids]

        plan.append(
            _RescoreEntry(
                document_id=d.id,
                filename=d.original_filename,
                statement_days=current.statement_days,
                before_true_revenue=current.true_revenue,
                after_true_revenue=new_true,
                before_monthly_revenue=current.monthly_revenue,
                after_monthly_revenue=new_monthly,
                before_debt_to_revenue=current.debt_to_revenue,
                after_debt_to_revenue=new_debt_to_rev,
                before_source_count=len(current.true_revenue_source_ids),
                after_source_count=len(new_source_ids),
                new_source_ids=new_source_ids,
            )
        )

    if not plan:
        print("Nothing to re-aggregate.")
        return 0

    # Render the diff
    print("Re-aggregation diff (per document)")
    print("=" * 100)
    for p in plan:
        print(f"  {p.filename}")
        print(f"    document_id          = {p.document_id}")
        print(f"    statement_days       = {p.statement_days}")
        print(
            "    true_revenue         : "
            f"{_fmt_money(p.before_true_revenue)}  ->  "
            f"{_fmt_money(p.after_true_revenue)}"
        )
        print(
            "    monthly_revenue      : "
            f"{_fmt_money(p.before_monthly_revenue)}  ->  "
            f"{_fmt_money(p.after_monthly_revenue)}"
        )
        print(
            "    debt_to_revenue      : "
            f"{p.before_debt_to_revenue!s:>16}  ->  "
            f"{p.after_debt_to_revenue!s:>16}"
        )
        print(
            "    true_revenue source_ids count : "
            f"{p.before_source_count:>4}  ->  {p.after_source_count:>4}"
        )
        print()

    # Summary
    after_sum_revenue = sum(
        (p.after_true_revenue for p in plan), start=Decimal("0")
    )
    print(
        f"Summed new true_revenue across {len(plan)} docs: "
        f"{_fmt_money(after_sum_revenue)}"
    )
    print()

    # Hard YES gate
    answer = input("Type YES (exact) to write these updates to prod: ")
    if answer.strip() != "YES":
        print("Aborted — no writes performed.")
        return 2

    # Perform updates
    ts = datetime.now(UTC).isoformat()
    written = 0
    for p in plan:
        sb.table("analyses").update(
            {
                "true_revenue": str(p.after_true_revenue),
                "monthly_revenue": str(p.after_monthly_revenue),
                "debt_to_revenue": str(p.after_debt_to_revenue),
                "true_revenue_source_ids": p.new_source_ids,
            }
        ).eq("document_id", str(p.document_id)).execute()

        audit.record(
            actor="rescore_vu",
            actor_email=actor_email,
            action="analysis.reaggregated",
            subject_type="document",
            subject_id=p.document_id,
            details={
                "merchant_id": str(merchant_id),
                "reason": "h1_true_revenue_fix",
                "before_true_revenue": str(p.before_true_revenue),
                "after_true_revenue": str(p.after_true_revenue),
                "before_monthly_revenue": str(p.before_monthly_revenue),
                "after_monthly_revenue": str(p.after_monthly_revenue),
                "rescored_at": ts,
            },
        )
        written += 1
        print(f"  wrote {p.filename}")

    print(f"\nDone — {written} analyses updated.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: rescore_vu.py <merchant_id> <actor_email>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
