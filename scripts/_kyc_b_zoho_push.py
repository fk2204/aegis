"""KYC merchant — phase B: push to Zoho Deal + run submission + verify.

Sequence:
  B1. Compute ScoreResult locally → POST /deals/{mid}/sync-to-zoho?target=deal
      (creates the Zoho Deal record, persists merchant.zoho_deal_id).
  B2. POST /ui/merchants/{mid}/submit with the 3 active funder IDs as form
      fields. The route mirrors the submission to Zoho — bumps each
      Lender's Total_Submissions, sets Last_Submission_Date, attaches the
      submission ZIP to the Deal.
  B3. Read the Zoho Deal + each Lender back via the same ZohoClient and
      print the fields the operator cares about for visual confirmation.

Run on the box (loads /etc/aegis/aegis.env for API_BEARER_TOKEN + Zoho creds):

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python /tmp/_kyc_b_zoho_push.py
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote
from uuid import UUID

import httpx

from aegis.api.deps import (
    get_funder_repository,
    get_merchant_repository,
    get_repository,
)
from aegis.scoring.score import score_deal
from aegis.web.router import _score_input_from_dashboard
from aegis.zoho.client import ZohoClient

_MERCHANT_ID = UUID("c21b7f76-bbf8-4880-a090-15e6fe9d6c85")
_BASE = "http://127.0.0.1:5555"
_BEARER = os.environ.get("API_BEARER_TOKEN") or sys.exit("missing API_BEARER_TOKEN")
_HDRS = {"Authorization": f"Bearer {_BEARER}"}


def _b1_push_to_zoho() -> str:
    merchants = get_merchant_repository()
    docs = get_repository()

    merchant = merchants.get(_MERCHANT_ID)
    docs_list = docs.list_documents(merchant_id=_MERCHANT_ID, limit=1)
    if not docs_list:
        sys.exit("no documents for merchant")
    latest = docs_list[0]
    analysis = docs.get_analysis(latest.id)
    if analysis is None:
        sys.exit("no analysis for latest doc")

    score_input = _score_input_from_dashboard(merchant, latest, analysis)
    score = score_deal(score_input, ofac=None)
    print(
        f"  computed score: tier={score.tier} score={score.score} "
        f"rec={score.recommendation} suggested_max=${score.suggested_max_advance}"
    )

    print("  POST /deals/.../sync-to-zoho?target=deal")
    resp = httpx.post(
        f"{_BASE}/deals/{_MERCHANT_ID}/sync-to-zoho?target=deal",
        json=score.model_dump(mode="json"),
        headers=_HDRS,
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        sys.exit(f"sync-to-zoho FAILED {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    print(f"    → zoho_record_id={data['zoho_record_id']} action={data['action']}")
    return str(data["zoho_record_id"])


def _b2_submit() -> list[tuple[str, str]]:
    funders = get_funder_repository()
    active = list(funders.list_active())
    pairs = [(str(f.id), f.name) for f in active]
    print(f"  selecting {len(pairs)} funder(s): {[n for _, n in pairs]}")

    form = {"funder_ids": [fid for fid, _ in pairs]}
    resp = httpx.post(
        f"{_BASE}/ui/merchants/{_MERCHANT_ID}/submit",
        data=form,
        headers=_HDRS,
        timeout=120,
        follow_redirects=False,
    )
    if resp.status_code != 200:
        sys.exit(f"submit FAILED {resp.status_code}: {resp.text[:500]}")
    cd = resp.headers.get("content-disposition", "<none>")
    print(f"  status=200 body_bytes={len(resp.content)} content-disposition={cd}")
    return pairs


def _b3_verify(deal_id: str, funder_pairs: list[tuple[str, str]]) -> None:
    with ZohoClient() as client:
        print(f"  Deal /crm/v8/Deals/{deal_id}")
        deal_resp = client.request("GET", f"/crm/v8/Deals/{deal_id}")
        rows = deal_resp.get("data") or []
        if not rows:
            print("    NO DATA returned for deal")
            return
        deal = rows[0]
        print(f"    Deal_Name                : {deal.get('Deal_Name')}")
        print(f"    Date_Submitted_to_Lenders: {deal.get('Date_Submitted_to_Lenders')}")
        lst = deal.get("Lenders_Submitted_To")
        if isinstance(lst, list):
            names = [
                x.get("name") if isinstance(x, dict) else str(x) for x in lst
            ]
            print(f"    Lenders_Submitted_To     : {names}")
        else:
            print(f"    Lenders_Submitted_To     : {lst}")

        print()
        print("  Per-Lender check:")
        for _, name in funder_pairs:
            try:
                body = client.request(
                    "GET",
                    f"/crm/v8/Lenders/search?criteria={quote('(Name:equals:' + name + ')')}",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"    {name:30}: lookup error {exc}")
                continue
            data = body.get("data") or []
            if not data:
                print(f"    {name:30}: NOT FOUND in Zoho Lenders")
                continue
            L = data[0]
            print(
                f"    {name:30}: "
                f"Total_Submissions={L.get('Total_Submissions')!r} "
                f"Last_Submission_Date={L.get('Last_Submission_Date')!r}"
            )


def main() -> int:
    print("=== B1: sync-to-zoho (target=deal) ===")
    deal_id = _b1_push_to_zoho()
    print()
    print("=== B2: /ui submit (mirrors to Zoho Lenders + attaches ZIP) ===")
    funder_pairs = _b2_submit()
    print()
    print("=== B3: verify Zoho Deal + Lender state ===")
    _b3_verify(deal_id, funder_pairs)
    print()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
