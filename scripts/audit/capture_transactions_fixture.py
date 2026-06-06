"""Capture real transactions from prod into a sanitized test fixture.

This is the permanent, committed version of the ad-hoc capture script
that produced ``tests/counterparty/fixtures/vu_real_txns.json``. The
historical mistake (2026-06-05, commit ``ae62df2``) shipped a
captured fixture WITH PII because the sanitization pass ran AFTER the
fixture was already written to disk and the regex missed the
"Zelle payment to" rows.

This script bakes the sanitization into the write path so a raw-PII
fixture **cannot** be produced. The ``tests/_fixture_sanitize`` module
holds the redaction logic, and ``tests/test_fixture_pii_canary`` is the
forward-protection that fails CI if any committed fixture has known
PII patterns.

Workflow
--------

1. Operator runs this script on the box (Supabase creds available):

   .venv/bin/python scripts/audit/capture_transactions_fixture.py \\
       --merchant-id 5cf4479d-c6ac-4267-a2f7-5e7ef04c1345 \\
       --output /tmp/sanitized_merchant.json

2. Operator SCPs the sanitized JSON down to the laptop, places it
   under ``tests/<domain>/fixtures/`` with a descriptive name, and
   commits.

3. The PII canary runs on every CI build and catches any future
   regression (or any new fixture committed without going through
   the sanitizer).

This script REFUSES to write the output until the sanitizer has run
and ``assert_no_pii_in_descriptions`` confirms no known PII patterns
remain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

# This script must be runnable from the box; the sanitizer is a
# tests/-only module so we extend sys.path to import it.
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tests._fixture_sanitize import (  # noqa: E402
    assert_no_pii_in_descriptions,
    sanitize_fixture_payload,
)


def _fetch_for_merchant(merchant_id: str) -> dict[str, Any]:
    """Pull documents + transactions for one merchant from Supabase."""
    from aegis.db import get_supabase

    sb = get_supabase()
    docs = cast(
        list[dict[str, Any]],
        sb.table("documents")
        .select("id,original_filename,parse_status")
        .eq("merchant_id", merchant_id)
        .execute()
        .data
        or [],
    )
    out_docs: list[dict[str, Any]] = []
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
        txns_raw = cast(
            list[dict[str, Any]],
            sb.table("transactions")
            .select(
                "id,posted_date,description,amount,category,"
                "running_balance"
            )
            .eq("document_id", d["id"])
            .order("posted_date")
            .execute()
            .data
            or [],
        )
        out_docs.append(
            {
                "document_id": d["id"],
                "filename": d.get("original_filename") or "",
                "parse_status": d.get("parse_status"),
                "summary": {
                    "bank_name": a.get("bank_name"),
                    "account_last4": last4,
                    "period_start": a.get("statement_period_start"),
                    "period_end": a.get("statement_period_end"),
                },
                "transactions": [
                    {
                        "id": t["id"],
                        "posted_date": t["posted_date"],
                        "description": t["description"],
                        "amount": str(t["amount"]),
                        "category": t.get("category"),
                        "running_balance": (
                            str(t["running_balance"])
                            if t.get("running_balance") is not None
                            else None
                        ),
                    }
                    for t in txns_raw
                ],
            }
        )
    return {"merchant_id": merchant_id, "documents": out_docs}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--merchant-id", required=True, type=str)
    p.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Path to write the sanitized fixture JSON (typically /tmp/…).",
    )
    args = p.parse_args()

    print(f"capturing transactions for merchant {args.merchant_id}...")
    raw = _fetch_for_merchant(args.merchant_id)
    n_docs = len(raw.get("documents", []))
    n_txns = sum(
        len(d.get("transactions") or []) for d in raw.get("documents", [])
    )
    print(f"  captured: {n_docs} documents, {n_txns} transactions")

    print("running sanitizer...")
    sanitized = sanitize_fixture_payload(raw)

    # Belt-and-suspenders: confirm the canary patterns are absent
    # BEFORE writing to disk. If this raises, the file is never
    # written and the operator gets a clear error.
    print("running canary check before write...")
    assert_no_pii_in_descriptions(sanitized)
    print("  canary clean")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sanitized, indent=2))
    print(f"wrote sanitized fixture: {args.output}")
    print(
        "\nnext step: scp this file down to the laptop, place it under "
        "tests/<domain>/fixtures/ with a descriptive name, and commit. "
        "The PII canary test in tests/test_fixture_pii_canary.py will "
        "catch any future regression."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
