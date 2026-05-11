"""Accuracy v2 smoke test — re-parse the real bank-statement corpus.

Goal
----
Validate that the new accuracy gates from commit ``02f784c``
(classification-confidence floor, intra-day balance, row-count
parity) don't false-positive on real operator-supplied bank
statements, and inform whether ``CLASSIFICATION_CONFIDENCE_FLOOR =
60`` needs tuning.

Flow
----
1. Snapshot the prior production parse for each PDF by ``file_hash``
   (``parse_status``, ``fraud_score``, ``all_flags``).
2. Create a fresh ``_smoke_test_<run_id>_<n>_<slug>`` merchant per
   PDF so the audit data is isolated from production rows.
3. Append a one-line PDF comment (``\\n% audit-reparse ...``) to
   each upload so the SHA-256 dedup check misses → fresh
   ``document_id``, fresh parse run. The original
   ``/var/lib/aegis/audit-pdfs/`` files are NOT modified.
4. POST ``/upload`` via the bearer API with ``merchant_id``
   pointing at the smoke-test merchant.
5. Poll Supabase every 5 s until all parses reach terminal state
   (``proceed`` / ``review`` / ``manual_review`` / ``parse_failed``)
   or the 20-minute timeout fires.
6. Print a before/after parse_status comparison table + a tuning
   recommendation grounded in the moved-to-manual_review count.

Cleanup (after operator review)
-------------------------------
The script prints the exact ``DELETE`` query at the end. All
smoke-test rows are findable by
``business_name LIKE '_smoke_test_<run_id>%'`` — the run_id is
embedded so multiple runs don't collide.

Invocation
----------
Run on the box (root, env loaded from /etc/aegis/aegis.env):

    sudo bash -c 'set -a && source /etc/aegis/aegis.env && set +a \\
        && cd /opt/aegis \\
        && uv run python scripts/audit/reparse_real_pdfs.py'

Required env (set via aegis.env):
  - ``AEGIS_DATA_RESIDENCY_CONFIRMED=true``
  - ``SUPABASE_URL``, ``SUPABASE_SERVICE_KEY``
  - ``API_BEARER_TOKEN``
  - ``BEDROCK_MODEL_ID``, ``AWS_*`` credentials
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from aegis.db import get_supabase

PDF_DIR = Path("/var/lib/aegis/audit-pdfs")
AEGIS_URL = "http://127.0.0.1:5555"
POLL_INTERVAL_S = 5
TIMEOUT_S = 20 * 60  # 20 minutes
TERMINAL = frozenset({"proceed", "review", "manual_review", "parse_failed"})


def main() -> int:
    bearer = os.environ.get("API_BEARER_TOKEN")
    if not bearer:
        print("ERROR: API_BEARER_TOKEN env var required", file=sys.stderr)
        return 2

    if not PDF_DIR.is_dir():
        print(f"ERROR: {PDF_DIR} does not exist", file=sys.stderr)
        print("  Operator: mkdir -p /var/lib/aegis/audit-pdfs && rsync the PDFs", file=sys.stderr)
        return 2

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"ERROR: no PDFs found in {PDF_DIR}", file=sys.stderr)
        return 2

    print(f"Found {len(pdfs)} PDF(s) in {PDF_DIR}")
    sb = get_supabase()

    # 1. Snapshot prior parse state.
    print("\n[1/5] Snapshotting prior parse state by file_hash…")
    before: dict[str, dict[str, Any]] = {}
    for pdf in pdfs:
        file_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
        rows = cast(
            list[dict[str, Any]],
            sb.table("documents")
            .select("id, parse_status, fraud_score, all_flags")
            .eq("file_hash", file_hash)
            .execute()
            .data
            or [],
        )
        before[pdf.name] = (
            rows[0]
            if rows
            else {
                "id": None,
                "parse_status": "(new)",
                "fraud_score": None,
                "all_flags": [],
            }
        )
    matched = sum(1 for r in before.values() if r["id"] is not None)
    print(f"      matched {matched}/{len(pdfs)} PDFs to existing prod rows")

    # 2. Create smoke-test merchants + upload modified PDFs.
    run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    print(f"\n[2/5] Creating smoke-test merchants (run_id={run_id})…")
    smoke_docs: dict[str, str] = {}  # pdf.name -> document_id
    with httpx.Client(timeout=180) as http:
        for idx, pdf in enumerate(pdfs):
            slug = pdf.stem[:24].replace(" ", "_").replace(".", "_")
            biz_name = f"_smoke_test_{run_id}_{idx:02d}_{slug}"
            m_resp = (
                sb.table("merchants")
                .insert(
                    {
                        "business_name": biz_name,
                        "owner_name": "(smoke test - auto cleanup)",
                        "state": "CA",
                        "broker_source": "audit reparse_real_pdfs.py",
                        "intake_date": datetime.now(UTC).date().isoformat(),
                    }
                )
                .execute()
            )
            merchant_id = cast(list[dict[str, Any]], m_resp.data or [])[0]["id"]

            # Append a marker line so SHA-256 dedup misses; PDF parsers
            # ignore anything after %%EOF, so the bytes still validate.
            original = pdf.read_bytes()
            marker = f"\n% audit-reparse {run_id} idx={idx:02d}\n".encode()
            modified = original + marker

            response = http.post(
                f"{AEGIS_URL}/upload",
                headers={"Authorization": f"Bearer {bearer}"},
                files={"file": (pdf.name, modified, "application/pdf")},
                data={"merchant_id": merchant_id},
            )
            if response.status_code not in (200, 202):
                print(
                    f"      upload FAILED {pdf.name}: "
                    f"{response.status_code} {response.text[:200]}",
                    file=sys.stderr,
                )
                continue
            doc_id = response.json()["document_id"]
            smoke_docs[pdf.name] = doc_id
            print(
                f"      [{idx + 1:2d}/{len(pdfs)}] "
                f"{pdf.name[:38]:38s} -> doc {doc_id[:8]}..."
            )

    if not smoke_docs:
        print("ERROR: no successful uploads", file=sys.stderr)
        return 1

    # 3. Poll for terminal status.
    print(f"\n[3/5] Polling Supabase every {POLL_INTERVAL_S}s for parse completion…")
    start = time.monotonic()
    final_rows: dict[str, dict[str, Any]] = {}
    while True:
        rows = cast(
            list[dict[str, Any]],
            sb.table("documents")
            .select("id, parse_status, fraud_score, all_flags")
            .in_("id", list(smoke_docs.values()))
            .execute()
            .data
            or [],
        )
        final_rows = {r["id"]: r for r in rows}
        unfinished = [
            doc_id
            for doc_id in smoke_docs.values()
            if final_rows.get(doc_id, {}).get("parse_status") not in TERMINAL
        ]
        elapsed = time.monotonic() - start
        if not unfinished:
            print(f"      all {len(smoke_docs)} docs reached terminal state in {elapsed:.0f}s")
            break
        if elapsed > TIMEOUT_S:
            print(
                f"      TIMEOUT after {elapsed:.0f}s — "
                f"{len(unfinished)}/{len(smoke_docs)} still parsing",
                file=sys.stderr,
            )
            break
        print(
            f"      {elapsed:5.0f}s   "
            f"{len(smoke_docs) - len(unfinished)}/{len(smoke_docs)} done"
        )
        time.sleep(POLL_INTERVAL_S)

    # 4. Render comparison table.
    print("\n[4/5] Comparison table\n")
    header = (
        f"{'PDF':40s}  {'BEFORE':16s}  {'AFTER':16s}  "
        f"{'fraud':>6s}  {'flags':>5s}"
    )
    print(header)
    print("-" * len(header))

    before_dist: dict[str, int] = {}
    after_dist: dict[str, int] = {}
    moved_to_mr = 0
    moved_proceed_to_review = 0

    for pdf in pdfs:
        b = before[pdf.name]
        doc_id = smoke_docs.get(pdf.name)
        a = final_rows.get(doc_id, {}) if doc_id else {}
        b_status = b.get("parse_status") or "(new)"
        a_status = a.get("parse_status") or "(upload-failed)"
        a_score = a.get("fraud_score")
        a_flags = len(a.get("all_flags") or [])
        before_dist[b_status] = before_dist.get(b_status, 0) + 1
        after_dist[a_status] = after_dist.get(a_status, 0) + 1
        if b_status == "proceed" and a_status == "manual_review":
            moved_to_mr += 1
        if b_status == "proceed" and a_status == "review":
            moved_proceed_to_review += 1
        score_str = f"{a_score}" if a_score is not None else "-"
        print(
            f"{pdf.name[:40]:40s}  "
            f"{b_status:16s}  {a_status:16s}  "
            f"{score_str:>6s}  {a_flags:>5d}"
        )

    print("-" * len(header))
    print(f"\nDistribution BEFORE: {dict(sorted(before_dist.items()))}")
    print(f"Distribution AFTER:  {dict(sorted(after_dist.items()))}")
    print("\nMovement:")
    print(f"  proceed -> manual_review : {moved_to_mr}/{len(pdfs)}")
    print(f"  proceed -> review        : {moved_proceed_to_review}/{len(pdfs)}")

    # 5. Tuning recommendation.
    print("\n[5/5] CLASSIFICATION_CONFIDENCE_FLOOR = 60 tuning recommendation")
    print("-" * 72)
    if moved_to_mr == 0:
        print(
            "  0/14 moved proceed -> manual_review.\n"
            "  -> Floor of 60 is either non-binding on this corpus OR the\n"
            "     statements really are clean. Inspect per-statement\n"
            "     avg_classification_confidence on each AFTER doc:\n"
            "       SELECT id, avg_classification_confidence\n"
            "       FROM analyses WHERE document_id IN (...);\n"
            "     If all >> 60 (e.g. 85+), the gate is healthy but quiet.\n"
            "     If many are 60-65, consider TIGHTENING to 70 to catch\n"
            "     borderline parses before they reach the operator."
        )
    elif moved_to_mr <= 4:
        print(
            f"  {moved_to_mr}/14 moved proceed -> manual_review.\n"
            "  -> HEALTHY SIGNAL. Floor of 60 is catching real low-confidence\n"
            "     parses without over-triggering. KEEP CURRENT VALUE."
        )
    else:
        print(
            f"  {moved_to_mr}/14 moved proceed -> manual_review — TOO MANY.\n"
            "  -> Floor of 60 may be over-strict. Two options:\n"
            "       (a) tune CLASSIFICATION_CONFIDENCE_FLOOR DOWN to 50\n"
            "           and re-run this script,\n"
            "       (b) keep the avg floor at 60 but RAISE the per-category\n"
            "           mca_debit floor (currently 70) so the gate concentrates\n"
            "           on high-impact misclassifications.\n"
            "     Choose (b) if the failures are mostly fee/other rows."
        )

    # Operator-facing guidance text. The DELETE below is printed for the
    # operator to paste into the Supabase SQL editor — the script does NOT
    # execute it. run_id is a UTC timestamp generated above, not user input.
    cleanup_sql = (
        f"DELETE FROM merchants WHERE business_name LIKE '_smoke_test_{run_id}%';"  # noqa: S608
    )
    print(
        "\nCleanup (paste into Supabase SQL editor):\n"
        f"  {cleanup_sql}\n"
        "  (documents/transactions/analyses cascade if FK ON DELETE CASCADE\n"
        "  is set; otherwise wipe them with the same WHERE on merchant_id.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
