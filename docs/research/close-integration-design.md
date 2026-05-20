# Close CRM Integration — Design

**Status:** Proposed (awaiting operator review before implementation)
**Branch:** `feature/close-integration`
**Date:** 2026-05-20
**Author:** Filip + Claude

---

## Why

Commera is moving from Zoho CRM to Close. AEGIS currently writes to Zoho Deals + Leads + Lender records and consumes a Zoho webhook for merchant upserts (`src/aegis/zoho/`, `src/aegis/api/routes/webhooks_zoho.py`). This branch replaces that integration with Close — the operator's chosen CRM going forward.

The Close org is already partially configured by the operator:

- A **"Sales" pipeline** modeling the MCA workflow (Discovery → Docs In — Pre-UW → Underwriting → UW Hold → Lender Shopping → Submitted → Offers In → Contract Out → Contract Signed → Won; plus Renewal Eligible / Dead — Merchant / Dead — Lender / Dead — UW Fail)
- A second "Zoho Pipeline" exists as a legacy mirror — **not** the integration target
- ~85 Lead-level custom fields covering identity, business profile, bank profile, MCA exposure, credit, history, and lead workflow
- **5 Aegis-specific Lead fields already provisioned**: `Aegis Applicant ID`, `Aegis Score`, `Aegis Recommendation` (Approve/Decline/Refer), `OFAC Status` (Clear/Flagged/Pending), `Aegis Last Synced`
- 3 Custom Activity Types ready for funder activity: `Submission`, `Offer`, `Decline` (NOT used in this branch; deferred)
- 3 users: filip@, edward@, dima@ commerafunding.com
- No active workflows, no forms yet

## Operator decisions

1. **Hard cutover.** Zoho code, env vars, and DB columns are removed in this branch. One PR, one deploy, one rollback unit.
2. **Hybrid statement intake.** Existing `/upload` endpoint stays. Additionally accept a Close attachment reference — AEGIS pulls the file via Close's attachment API. SHA256 dedup either path.
3. **Minimal outbound surface.** AEGIS writes only the 4 Aegis-* Lead custom fields (Score, Recommendation, OFAC Status, Last Synced) plus `Aegis Applicant ID` once. **No** custom activities. **No** pipeline stage transitions. The operator drives the pipeline; AEGIS provides the data.

These decisions preserve the CLAUDE.md §1 boundary: AEGIS is the brain, Close is the CRM.

---

## Architecture (one paragraph)

A Close webhook subscription on `opportunity.status_change` (or `lead.updated`, depending on what Close exposes — verify during implementation) fires when the operator moves an Opportunity to **Docs In — Pre-UW**. AEGIS's `POST /webhooks/close` endpoint verifies HMAC-SHA256 + freshness, pulls the linked Lead's identity + intake fields from Close, upserts an AEGIS `merchants` row keyed by `close_lead_id`, and enqueues a parse if statements are attached to the Lead (hybrid path) or waits for an operator upload. Operator-uploaded statements (existing `/upload` endpoint) continue to work — both paths converge on `documents.id` with SHA256 dedup. After scoring completes, a new `push_decision_to_close()` PATCHes the 4 Aegis-* custom fields back to the Lead. The operator drives next-step pipeline transitions in Close based on what they see.

---

## Data mapping

### Close Lead → AEGIS `merchants`

| Close custom field | AEGIS column | Type | Notes |
|---|---|---|---|
| `Legal Name` | `business_name` | text | Required for disclosure rendering |
| `DBA Name` | `dba` | text | Optional |
| `EIN` | `ein` | text | "00-0000000" per Close field description |
| `Owner Name` | `owner_name` | text | PII — mask in logs |
| `State` | `state` | choice(50) | 2-letter code; drives compliance routing |
| `Industry` | (lookup → NAICS) | choice (approved list) | See `field_map.py` for Industry→NAICS table |
| `NAICS Code` | `industry_naics` | text(6) | Preferred when set; overrides Industry-derived value |
| `Time in Business (months)` | `time_in_business_months` | number | |
| `FICO Range` | `credit_score` | choice → int | "650-699" → 650 (lower-bound conservative; configurable) |
| `Requested Amount` | `requested_amount` | text → Decimal | Strip "$" / "," |
| `Entity type` / `Entity_type` | `entity_type` | choice | **Two duplicate fields exist in Close.** Read both, prefer non-null, log if both populated and disagree |
| `Existing MCA Positions` | (analysis cross-check) | number | Compare against parser detection; flag mismatch |
| `Existing MCA Balance` | (analysis cross-check) | number | Compare against parser detection; flag mismatch |
| `Avg Monthly Revenue` | (sanity check) | number | Used only to validate parsed revenue is in ballpark |

### AEGIS → Close Lead

| Source | Close field | Type | Value |
|---|---|---|---|
| `decisions.id` (latest per deal) | `Aegis Applicant ID` | text | Stable AEGIS deal identifier the operator pastes into AEGIS dashboard |
| `decisions.score` | `Aegis Score` | number | 0–100 |
| `decisions.decision` | `Aegis Recommendation` | choice | approve→Approve, decline→Decline, refer/manual_review→Refer |
| (computed) | `OFAC Status` | choice | Clear / Flagged / Pending — derived from `decisions.ofac_cache_timestamp` + decision flags |
| `datetime.now(UTC)` | `Aegis Last Synced` | datetime | Updated on every successful PATCH |

### DB schema change

```sql
-- migrations/026_close_lead_id.sql
ALTER TABLE merchants ADD COLUMN close_lead_id TEXT UNIQUE;
CREATE INDEX idx_merchants_close_lead_id ON merchants (close_lead_id)
  WHERE close_lead_id IS NOT NULL;
ALTER TABLE merchants DROP COLUMN zoho_deal_id;
ALTER TABLE merchants DROP COLUMN zoho_lead_id;
```

Both Zoho columns are currently nullable and not foreign-keyed; the drop is safe.

---

## Implementation steps

Each step is its own commit on `feature/close-integration`. The branch is not deployable until all 12 land.

1. **Migration** — `migrations/026_close_lead_id.sql`. Add `close_lead_id`, drop `zoho_deal_id` + `zoho_lead_id`. Probe in `scripts/apply_migrations.py`.
2. **Close client** — `src/aegis/close/client.py`. API-key auth (HTTP Basic, key as username, blank password — Close's standard pattern). Retrying GET/PATCH/POST via httpx + tenacity (3 attempts, exponential backoff on 429/5xx). Methods: `get_lead`, `update_lead_custom_fields`, `get_opportunity`, `download_attachment`.
3. **Field map** — `src/aegis/close/field_map.py`. Pure functions: Close payload → AEGIS row, AEGIS values → Close payload. FICO Range parser, Industry → NAICS lookup, Entity type dedup, money-text → Decimal.
4. **Inbound webhook** — `src/aegis/api/routes/webhooks_close.py`. `POST /webhooks/close`. HMAC-SHA256 via `X-Close-Signature` + 5-minute freshness (same pattern as `funder_replies.py:webhook_funder_reply`). Pull Lead via client, upsert `merchants` keyed by `close_lead_id`.
5. **Outbound write-back** — `src/aegis/close/sync.py`. `push_decision_to_close(close_lead_id, decision, ofac_status)`. One PATCH. Audit row: `close.lead.decision_pushed`.
6. **Operator-triggered sync route** — `src/aegis/api/routes/deals.py`. Replace `/deals/{merchant_id}/sync-to-zoho` with `/deals/{merchant_id}/sync-to-close`.
7. **Hybrid statement path** — `src/aegis/api/routes/upload.py` add optional `close_lead_id` query param. Add `POST /uploads/from-close` that fetches via `CloseClient.download_attachment()`. Both paths converge on `documents.id`.
8. **Zoho deletion (hard cutover)** — delete `src/aegis/zoho/`, `src/aegis/api/routes/webhooks_zoho.py`, `tests/test_zoho.py`, all Zoho helper usages, `ZOHO_*` env vars from `.env.example` and `deploy/aegis.env.example`.
9. **Config** — `src/aegis/config.py`: add `CLOSE_API_KEY`, `CLOSE_WEBHOOK_SECRET`, `CLOSE_API_BASE` (default `https://api.close.com`). Boot warning if any `ZOHO_*` env var lingers.
10. **Tests** — `tests/test_close_client.py`, `tests/test_close_field_map.py`, `tests/test_close_sync.py`, `tests/api/test_webhooks_close.py`. Target ≥20 new tests.
11. **Documentation + memory** — update `CLAUDE.md` Tech Stack (Zoho → Close); add memory `feedback-aegis-close-intake.md` superseding `feedback-aegis-zoho-intake.md`.
12. **Deploy** — push branch, await merge approval, then standard chain: local merge → push main → deploy → smoke check.

---

## Reused patterns (don't reinvent)

- **HMAC + freshness webhook verification** — copy `funder_replies.py` (and `webhooks_zoho.py`) pattern: HMAC-SHA256 over raw body, `hmac.compare_digest`, 5-minute freshness, 401 on bad sig, no log of secret value.
- **HTTP client with retry** — model after `src/aegis/zoho/client.py` but simpler (API key, no token refresh). Tenacity retry shape: 3 attempts, exponential backoff on 429/5xx.
- **Decimal-as-string serialization** — mirror `src/aegis/zoho/sync.py` for money fields. `str(Decimal(...))`, never `float`.
- **Audit-write-failure-raises** — match `src/aegis/audit.py` contract. Failed `audit.record()` propagates and fails the operation.
- **Idempotent upsert keyed by external ID** — model after `ZohoSync.apply_inbound()` in `src/aegis/zoho/sync.py`, key on `close_lead_id` instead.
- **Webhook → arq enqueue (if async needed)** — match the `process_funder_reply` worker pattern. First cut: do underwriting trigger sync from the webhook handler. Refactor to arq only if call volume warrants.

---

## Verification

1. **Unit tests** — `mypy src tests scripts` clean; `ruff check src tests scripts` clean; `pytest -q` shows ≥20 new Close tests and no regressions vs current main (~2172 passing).
2. **Migration dry-run** — `python scripts/apply_migrations.py --target prod --dry-run` shows exactly one pending migration: `026_close_lead_id.sql`. Real apply gated on operator approval.
3. **Inbound webhook smoke** — bad signature → 401; signed payload → 204 + `merchants` row created with `close_lead_id` set.
4. **Outbound smoke** — manually trigger `/deals/{merchant_id}/sync-to-close` against a test Lead in Close, verify the 4 Aegis-* fields populate and `Aegis Last Synced` is recent. Audit row lands in `audit_log`.
5. **Worker startup line** — unchanged: `parse_document, process_funder_reply, cron:run_archive_cron`. No new worker job in this branch.
6. **No Zoho references** — `grep -ri "zoho" src/ tests/ scripts/ .env.example` returns nothing (except possibly historical migration comments).

---

## Open questions (to be resolved during implementation, not blocking design)

1. **Webhook trigger event** — confirm Close emits a usable event when an Opportunity status changes to "Docs In — Pre-UW". If only `lead.updated` is available, the handler filters server-side on the status change. Make the trigger status configurable via env var.
2. **Industry → NAICS lookup table** — Close's "Industry" choice list doesn't map 1:1 to NAICS. Static table in `field_map.py`; operator validates.
3. **FICO Range → integer** — default to lower-bound conservative ("650-699" → 650). Configurable.
4. **Renewal handling** — Close has a "Renewal Eligible" status. When a renewal-eligible Lead gets a new Opportunity, does AEGIS re-underwrite from scratch, or skip if a recent decision exists? Defer to master plan Phase 6.
5. **Multiple Opportunities per Lead** — a Lead can have many Opportunities (one per deal, including renewals). Aegis-* fields live on the LEAD (operator's choice). For renewals this means the Lead fields get overwritten by the latest underwriting — by design. If the operator wants per-Opportunity history later, that's what the deferred custom activities (Submission/Offer/Decline) are for.
6. **Operator pipeline drive** — confirmed: AEGIS does NOT move pipeline stages. Reaffirmed here so a future contributor doesn't add "helpful" auto-transitions.

---

## Out of scope (explicitly deferred)

- **Submission / Offer / Decline custom activities.** Custom Activity Types exist in Close, but writing them is deferred. The operator currently sees funder activity inside AEGIS; Close timeline integration is a follow-up.
- **Pipeline stage automation.** AEGIS will not transition Opportunity statuses. The operator drives Discovery → Underwriting → Lender Shopping → ... manually.
- **Zoho-historical data migration.** The "Zoho Pipeline" mirror in Close already exists (operator-managed). This branch does not touch historical Zoho leads; they live where they live.
- **Per-Opportunity scoring history.** AEGIS fields are Lead-level. Renewals overwrite. Per-deal history will come when custom activities are added.
- **Bulk reconciliation tool.** If Close and AEGIS diverge (operator edits in Close, AEGIS doesn't see it until next webhook), there's no batch reconcile job. Out of scope.
