"""End-to-end pipeline test on real KYC statements (Know Your Collectibles Inc).

Runs the full Commera workflow:
  1. Create merchant via POST /merchants (bearer)
  2. Upload all PDFs in /tmp/kyc/ via POST /upload (bearer) — each
     enqueues a parse job for the worker.
  3. Poll the DB until every document reaches a terminal parse_status.
  4. Print per-document analysis summaries (deposits, NSF, ADB, etc.).
  5. Build a ScoreInput from the latest statement, score, run match_funder
     against the 3 production funders.
  6. Build the submission package (CSV/ZIP).

Read-only on the funder + lender tables. WRITES to merchants, documents,
transactions, analyses, audit_log — by design (the whole point is
exercising the real pipeline).

Run on the box:
    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python /tmp/_e2e_kyc_test.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from uuid import UUID

import httpx

from aegis.api.deps import (
    get_funder_repository,
    get_merchant_repository,
    get_repository,
)
from aegis.scoring.match_funders import match_funder
from aegis.scoring.score import score_deal
from aegis.scoring.submission_package import build_submission_files
from aegis.web.router import _score_input_from_dashboard

_BASE = "http://127.0.0.1:5555"
_TIMEOUT = 60.0
_BEARER = os.environ.get("API_BEARER_TOKEN") or sys.exit("missing API_BEARER_TOKEN")
_HEADERS = {"Authorization": f"Bearer {_BEARER}"}


def _create_merchant(client: httpx.Client) -> UUID:
    """Create Know Your Collectibles Inc as an AEGIS merchant."""
    payload = {
        "business_name": "Know Your Collectibles Inc",
        "owner_name": "Filip Kozina",  # placeholder; operator can edit
        "state": "FL",
        "industry_naics": "453998",  # all other misc retail
        "industry_risk_tier": "moderate",
        "time_in_business_months": 24,
    }
    resp = client.post(f"{_BASE}/merchants", json=payload, headers=_HEADERS)
    if resp.status_code == 409:
        # Already exists — find it via the list endpoint.
        existing = client.get(f"{_BASE}/merchants?state=FL", headers=_HEADERS).json()
        for m in existing:
            if m["business_name"] == payload["business_name"]:
                print(f"  reusing existing merchant {m['id']}")
                return UUID(m["id"])
        sys.exit("409 but couldn't find existing merchant by name")
    resp.raise_for_status()
    data = resp.json()
    print(f"  created merchant {data['id']}")
    return UUID(data["id"])


def _upload_pdfs(client: httpx.Client, merchant_id: UUID, pdf_dir: Path) -> list[UUID]:
    """Upload each PDF via /upload with the merchant_id form-field."""
    doc_ids: list[UUID] = []
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    for pdf in pdfs:
        body = pdf.read_bytes()
        files = {"file": (pdf.name, body, "application/pdf")}
        data = {"merchant_id": str(merchant_id)}
        resp = client.post(
            f"{_BASE}/upload", files=files, data=data, headers=_HEADERS
        )
        if resp.status_code not in (200, 202):
            print(f"  UPLOAD FAILED {pdf.name}: {resp.status_code} {resp.text[:200]}")
            continue
        out = resp.json()
        doc_ids.append(UUID(out["document_id"]))
        marker = "dup" if out["duplicate_of_existing"] else "new"
        print(
            f"  uploaded {pdf.name:60} → doc={out['document_id'][:8]} "
            f"status={out['parse_status']} ({marker})"
        )
    return doc_ids


def _wait_for_parse(doc_ids: list[UUID], timeout_s: int = 900) -> None:
    """Poll docs.list_documents until every doc has a terminal parse_status."""
    docs_repo = get_repository()
    start = time.time()
    terminal = {"proceed", "review", "manual_review", "error"}
    while time.time() - start < timeout_s:
        statuses: dict[str, int] = {}
        all_terminal = True
        for did in doc_ids:
            d = docs_repo.get_document(did)
            statuses[d.parse_status] = statuses.get(d.parse_status, 0) + 1
            if d.parse_status not in terminal:
                all_terminal = False
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>3}s] statuses={statuses}")
        if all_terminal:
            print(f"  all {len(doc_ids)} parsed in {elapsed}s")
            return
        time.sleep(15)
    sys.exit(f"timeout after {timeout_s}s — last statuses: {statuses}")


def _show_analyses(doc_ids: list[UUID]) -> None:
    docs_repo = get_repository()
    print(f"\n  {'period':25} {'status':18} {'deposits':>12} {'adb':>10} "
          f"{'nsf':>4} {'neg':>4} {'mca':>4}")
    print(f"  {'-'*25} {'-'*18} {'-'*12} {'-'*10} {'-'*4} {'-'*4} {'-'*4}")
    for did in doc_ids:
        d = docs_repo.get_document(did)
        a = docs_repo.get_analysis(did)
        if a is None:
            print(f"  {'(no analysis)':25} {d.parse_status:18}")
            continue
        period = f"{a.statement_period_start} → {a.statement_period_end}"
        print(
            f"  {period:25} {d.parse_status:18} "
            f"${a.true_revenue:>10} ${a.avg_daily_balance:>8} "
            f"{a.num_nsf:>4} {a.days_negative:>4} {a.mca_positions:>4}"
        )


def _run_match_and_submit(merchant_id: UUID) -> None:
    merchant_repo = get_merchant_repository()
    docs_repo = get_repository()
    funder_repo = get_funder_repository()
    merchant = merchant_repo.get(merchant_id)
    # latest doc by uploaded_at
    rows = docs_repo.list_documents(merchant_id=merchant_id, limit=1)
    latest_doc = rows[0]
    analysis = docs_repo.get_analysis(latest_doc.id)
    if analysis is None:
        print("\n  ** latest document has no analysis — skipping match **")
        return
    score_input = _score_input_from_dashboard(merchant, latest_doc, analysis)
    score = score_deal(score_input, ofac=None)
    print(
        f"\n  AEGIS SCORE: tier={score.tier} score={score.score} "
        f"rec={score.recommendation} suggested_max=${score.suggested_max_advance} "
        f"factor={score.recommended_factor_rate} payback={score.estimated_payback_days}d"
    )
    if score.hard_decline_reasons:
        print(f"  HARD DECLINES: {score.hard_decline_reasons}")
    if score.soft_concerns:
        print(f"  SOFT CONCERNS: {score.soft_concerns}")

    print("\n  FUNDER MATCHING:")
    matches = []
    for f in funder_repo.list_active():
        m = match_funder(f, score_input, score)
        if m is None:
            print(f"    SKIP   {f.name:30} (no configured criteria)")
            continue
        if m.match_score == 0:
            color = "🔴 RED"
        elif m.soft_concerns:
            color = "🟡 YELLOW"
        else:
            color = "🟢 GREEN"
        concerns = m.soft_concerns or ["—"]
        print(f"    {color:10} {f.name:30} score={m.match_score:3} concerns={concerns}")
        if m.match_score > 0:
            matches.append(m)

    if not matches:
        print("\n  ** No green/yellow matches — nothing to submit **")
        return

    print(f"\n  Building submission package for {len(matches)} matched funder(s)...")
    files = build_submission_files(score_input, score, matches)
    for sub in files:
        print(f"    {sub.filename} ({len(sub.csv_bytes)} bytes)")
        print(f"      preview: {sub.csv_bytes.decode().splitlines()[0:5]}")


def main() -> int:
    pdf_dir = Path("/tmp/kyc")  # noqa: S108 — operator-staged dir on the box
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"no PDFs found in {pdf_dir}")
    print("=== STEP 1: Create merchant ===")
    with httpx.Client(timeout=_TIMEOUT) as client:
        merchant_id = _create_merchant(client)
        print(f"\n=== STEP 2: Upload {len(pdfs)} statements ===")
        doc_ids = _upload_pdfs(client, merchant_id, pdf_dir)
    if not doc_ids:
        sys.exit("no successful uploads")
    print("\n=== STEP 3: Wait for worker to parse (Claude extraction + classification) ===")
    _wait_for_parse(doc_ids, timeout_s=900)
    print("\n=== STEP 4: Analysis summaries ===")
    _show_analyses(doc_ids)
    print("\n=== STEP 5: Score + match + submission package ===")
    _run_match_and_submit(merchant_id)
    print(f"\nMerchant URL: https://aegis.commerafunding.com/ui/merchants/{merchant_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
