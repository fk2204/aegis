"""Smoke test: Aegis -> Zoho push pipeline.

Usage:
  AEGIS_BASE_URL=https://aegis.commerafunding.com \
  API_BEARER_TOKEN=xxx \
  ZOHO_ACCESS_TOKEN=xxx \
  python scripts/test_zoho_push.py <merchant_uuid> [--target lead|deal]

--target defaults to 'lead' (the natural early-pipeline target — website
form -> Lead -> enriched-by-Aegis -> rep converts -> Deal). Pass
'--target deal' to test the Deals push path instead.

Steps:
  1. POST /deals/score - get ScoreResult for the merchant
  2. POST /deals/{merchant_id}/sync-to-zoho?target=<lead|deal> - push
  3. GET Zoho /Leads/{id} or /Deals/{id} - verify required AEGIS fields populated

Exits 0 on full pipeline success, 1 on any step failure (with stderr detail).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

TIMEOUT_SECONDS = 30.0
ZOHO_LEAD_URL = "https://www.zohoapis.com/crm/v8/Leads/{record_id}"
ZOHO_DEAL_URL = "https://www.zohoapis.com/crm/v8/Deals/{record_id}"

# Lead-side AEGIS_* fields (created 2026-05-12). Casing differs from Deals
# because Zoho auto-generates api_name from field_label; the Lead fields
# were created with Title Case labels, the Deal fields with UPPERCASE.
REQUIRED_LEAD_FIELDS = (
    "Aegis_Applicant_ID",
    "Aegis_Score",
    "Aegis_Recommendation",
    "OFAC_Status",
    "Aegis_Last_Synced",
)

REQUIRED_DEAL_FIELDS = (
    "AEGIS_Score",
    "AEGIS_Tier",
    "AEGIS_Recommendation",
    "Suggested_Max_Advance",
)


SAMPLE_SCORE_INPUT: dict[str, Any] = {
    "merchant_id": "<placeholder>",
    "business_name": "Acme Test LLC",
    "owner_name": "Test Owner",
    "state": "FL",
    "credit_score": 720,
    "time_in_business_months": 36,
    "avg_daily_balance": "8000.00",
    "true_revenue": "25000.00",
    "monthly_revenue": "25000.00",
    "lowest_balance": "1200.00",
    "num_nsf": 0,
    "days_negative": 0,
    "mca_positions": 0,
    "mca_daily_total": "0",
    "debt_to_revenue": "0",
    "payroll_detected": True,
    "returned_ach_count": 0,
    "statement_period_start": "2026-02-01",
    "statement_period_end": "2026-04-30",
    "statement_days": 89,
    "fraud_score": 5,
    "eof_markers": 1,
    "validation_passed": True,
    "extraction_confidence": 95,
    "requested_amount": "75000.00",
    "requested_factor": "1.35",
    "requested_term_days": 180,
    "is_renewal": False,
    "prior_advance_count": 0,
    "monthly_breakdown": [],
}


def die(step: str, message: str, response: httpx.Response | None = None) -> None:
    """Print failure detail to stderr and exit 1."""
    print(f"\n[FAIL] {step}: {message}", file=sys.stderr)
    if response is not None:
        print(f"  status: {response.status_code}", file=sys.stderr)
        body = response.text
        if len(body) > 4000:
            body = body[:4000] + "...[truncated]"
        print(f"  body:   {body}", file=sys.stderr)
    sys.exit(1)


def require_env(name: str) -> str:
    """Read a required env var or die."""
    value = os.environ.get(name)
    if not value:
        print(f"[FAIL] Missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def parse_args() -> tuple[str, str]:
    """Return (merchant_id, target) from argv or die. target defaults to 'lead'."""
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print(
            "Usage: python scripts/test_zoho_push.py <merchant_uuid> [--target lead|deal]",
            file=sys.stderr,
        )
        sys.exit(1)
    merchant_id = sys.argv[1].strip()

    target = "lead"
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 >= len(sys.argv):
            print("--target requires a value: lead or deal", file=sys.stderr)
            sys.exit(1)
        target = sys.argv[idx + 1].strip().lower()
        if target not in ("lead", "deal"):
            print(f"--target must be 'lead' or 'deal', got: {target}", file=sys.stderr)
            sys.exit(1)
    return merchant_id, target


def score_merchant(
    client: httpx.Client, base_url: str, bearer: str, merchant_id: str
) -> dict[str, Any]:
    """Step 1: call /deals/score and return the ScoreResult dict."""
    payload = dict(SAMPLE_SCORE_INPUT)
    payload["merchant_id"] = merchant_id
    url = f"{base_url.rstrip('/')}/deals/score"

    try:
        response = client.post(
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            json=payload,
        )
    except httpx.HTTPError as exc:
        die("Step 1 /deals/score", f"HTTP error: {exc}")
        raise  # unreachable

    if response.status_code >= 400:
        die("Step 1 /deals/score", "non-2xx response", response)

    try:
        body: dict[str, Any] = response.json()
        return body
    except json.JSONDecodeError as exc:
        die("Step 1 /deals/score", f"invalid JSON: {exc}", response)
        raise  # unreachable


def push_to_zoho(
    client: httpx.Client,
    base_url: str,
    bearer: str,
    merchant_id: str,
    score_result: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    """Step 2: call /deals/{merchant_id}/sync-to-zoho?target=<...> with the ScoreResult."""
    url = f"{base_url.rstrip('/')}/deals/{merchant_id}/sync-to-zoho"

    try:
        response = client.post(
            url,
            params={"target": target},
            headers={"Authorization": f"Bearer {bearer}"},
            json=score_result,
        )
    except httpx.HTTPError as exc:
        die("Step 2 /deals/sync-to-zoho", f"HTTP error: {exc}")
        raise

    if response.status_code >= 400:
        die("Step 2 /deals/sync-to-zoho", "non-2xx response", response)

    try:
        body: dict[str, Any] = response.json()
        return body
    except json.JSONDecodeError as exc:
        die("Step 2 /deals/sync-to-zoho", f"invalid JSON: {exc}", response)
        raise


def verify_zoho_record(
    client: httpx.Client, zoho_token: str, zoho_record_id: str, target: str
) -> dict[str, Any]:
    """Step 3: GET the Lead or Deal from Zoho and ensure required fields are non-null."""
    required: tuple[str, ...]
    if target == "lead":
        url = ZOHO_LEAD_URL.format(record_id=zoho_record_id)
        required = REQUIRED_LEAD_FIELDS
        label = "Lead"
    else:
        url = ZOHO_DEAL_URL.format(record_id=zoho_record_id)
        required = REQUIRED_DEAL_FIELDS
        label = "Deal"

    try:
        response = client.get(
            url,
            headers={"Authorization": f"Zoho-oauthtoken {zoho_token}"},
        )
    except httpx.HTTPError as exc:
        die(f"Step 3 Zoho {label} GET", f"HTTP error: {exc}")
        raise

    if response.status_code >= 400:
        die(f"Step 3 Zoho {label} GET", "non-2xx response", response)

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        die(f"Step 3 Zoho {label} GET", f"invalid JSON: {exc}", response)
        raise

    data = body.get("data")
    if not data or not isinstance(data, list):
        die(f"Step 3 Zoho {label} GET", f"unexpected payload shape: {body}")
    record: dict[str, Any] = data[0]

    missing = [f for f in required if record.get(f) in (None, "")]
    if missing:
        die(
            f"Step 3 Zoho {label} GET",
            f"required fields null/missing in Zoho {label}: {missing}",
        )

    return record


def main() -> None:
    merchant_id, target = parse_args()
    base_url = os.environ.get("AEGIS_BASE_URL", "http://localhost:8000")
    api_bearer = require_env("API_BEARER_TOKEN")
    zoho_token = require_env("ZOHO_ACCESS_TOKEN")

    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        print(f"[1/3] Scoring merchant {merchant_id}...")
        score_result = score_merchant(client, base_url, api_bearer, merchant_id)
        score = score_result.get("score")
        tier = score_result.get("tier")
        recommendation = score_result.get("recommendation")
        print(f"      -> score={score} tier={tier} recommendation={recommendation}")

        print(f"[2/3] Pushing to Zoho (target={target})...")
        push_result = push_to_zoho(
            client, base_url, api_bearer, merchant_id, score_result, target
        )
        zoho_record_id = push_result.get("zoho_record_id")
        response_target = push_result.get("target")
        action = push_result.get("action")
        if not zoho_record_id or not isinstance(zoho_record_id, str):
            die(
                "Step 2 /deals/sync-to-zoho",
                f"no zoho_record_id in response: {push_result}",
            )
            raise  # unreachable, narrows type
        assert isinstance(zoho_record_id, str)
        if response_target != target:
            die(
                "Step 2 /deals/sync-to-zoho",
                f"response target mismatch: sent={target} got={response_target}",
            )
        print(f"      -> zoho_record_id={zoho_record_id} action={action}")

        label = "Lead" if target == "lead" else "Deal"
        print(f"[3/3] Verifying {label} in Zoho...")
        record = verify_zoho_record(client, zoho_token, zoho_record_id, target)
        required = REQUIRED_LEAD_FIELDS if target == "lead" else REQUIRED_DEAL_FIELDS
        for field in required:
            print(f"      -> {field}={record.get(field)} OK")

    print("\nSmoke test passed.")
    print(
        f"summary: merchant_id={merchant_id} target={target} "
        f"zoho_record_id={zoho_record_id} action={action} "
        f"score={score} tier={tier} recommendation={recommendation}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
