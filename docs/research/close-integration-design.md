# Close CRM Integration — Design

**Status:** Approved (operator-accepted 2026-05-20; ready for implementation step 1)
**Branch:** `feature/close-integration`
**Date:** 2026-05-20
**Author:** Filip + Claude

---

## Why

Commera is moving from Zoho CRM to Close. AEGIS currently writes to Zoho Deals + Leads + Lender records and consumes a Zoho webhook for merchant upserts (`src/aegis/zoho/`, `src/aegis/api/routes/webhooks_zoho.py`). This branch replaces that integration with Close — the operator's chosen CRM going forward.

The Close org is already partially configured by the operator:

- A **"Sales" pipeline** modeling the MCA workflow (Discovery → Docs In — Pre-UW → Underwriting → UW Hold → Lender Shopping → Submitted → Offers In → Contract Out → Contract Signed → Won; plus Renewal Eligible / Dead — Merchant / Dead — Lender / Dead — UW Fail). Trigger stage for AEGIS underwriting is **"Docs In — Pre-UW"** with status id `stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI`.
- A second "Zoho Pipeline" exists as a legacy mirror — **not** the integration target
- ~85 Lead-level custom fields covering identity, business profile, bank profile, MCA exposure, credit, history, and lead workflow
- **5 Aegis-specific Lead fields already provisioned**: `Aegis Applicant ID`, `Aegis Score`, `Aegis Recommendation` (Approve/Decline/Refer), `OFAC Status` (Clear/Flagged/Pending), `Aegis Last Synced`
- 3 Custom Activity Types ready for funder activity: `Submission`, `Offer`, `Decline` (NOT used in this branch; deferred)
- 3 users: filip@, edward@, dima@ commerafunding.com
- No active workflows, no forms yet

## Decisions (operator-confirmed)

1. **Hard cutover.** Zoho code is removed in this branch. Zoho DB columns are **renamed** (not dropped) so prod data isn't lost — see Migration Safety below.
2. **Hybrid statement intake.** Existing `/upload` endpoint stays. Additionally accept a Close attachment reference — AEGIS pulls the file via Close's attachment API. SHA256 dedup either path.
3. **Minimal outbound surface.** AEGIS writes only the 4 Aegis-* Lead custom fields (Score, Recommendation, OFAC Status, Last Synced) plus `Aegis Applicant ID` once. **No** custom activities. **No** pipeline stage transitions. The operator drives the pipeline; AEGIS provides the data.
4. **AEGIS does not auto-transition pipeline stages.** Period. A future contributor must not add "helpful" auto-transitions.
5. **FICO Range → integer is lower-bound conservative, baked in (not configurable).** `"<550"` → 549, `"550-599"` → 550, `"600-649"` → 600, `"650-699"` → 650, `"700+"` → 700. If the operator later wants midpoint, that's a one-commit code change.
6. **Multi-Opportunity per Lead: Lead fields overwrite.** The Aegis-* fields live on the LEAD (operator's design). For a Lead with multiple Opportunities (renewals), the latest underwriting overwrites. Per-Opportunity history will be the deferred custom-activities work, not this branch.
7. **Renewal handling: re-underwrite from scratch.** Every new Opportunity that triggers `Docs In — Pre-UW` runs a fresh underwriting cycle. No "skip if recent decision exists" optimization in v1. Re-evaluate when operator has the volume data to justify caching.
8. **Industry → NAICS: static lookup table in `field_map.py`.** Close's 18 Industry choices map to NAICS codes via a hard-coded table; operator validates the mapping during implementation.

These decisions preserve the CLAUDE.md §1 boundary: AEGIS is the brain, Close is the CRM.

---

## Architecture (one paragraph)

The operator moves an Opportunity to **Docs In — Pre-UW** in Close. Close emits an `opportunity.updated` event to AEGIS's `POST /webhooks/close` endpoint (Close does not emit a separate "status changed" event — see Webhook Trigger below). AEGIS verifies the signature, filters server-side for `data.status_id == <Docs In — Pre-UW>` with `"status_id" in event.changed_fields`, pulls the linked Lead's identity + intake fields via `CloseClient.get_lead()`, upserts an AEGIS `merchants` row keyed by `close_lead_id`, and enqueues a parse if statements are attached to the Lead (hybrid path) or waits for an operator upload. Operator-uploaded statements (existing `/upload` endpoint) continue to work — both paths converge on `documents.id` with SHA256 dedup. After scoring completes, `push_decision_to_close()` PATCHes the 4 Aegis-* custom fields back to the Lead. The operator drives next-step pipeline transitions in Close based on what they see.

---

## Webhook trigger (resolved from Close developer docs)

Source: <https://developer.close.com/api/resources/webhooks.md> and <https://developer.close.com/api/resources/events/list-of-event-types.md>.

**Close emits these Opportunity events:**
- `opportunity.created`
- `opportunity.updated` — fires when any basic field changes (status, date_won, value, confidence) AND when custom fields change. Status changes are NOT a separate event; they fire as `opportunity.updated` with `status_id` in the `changed_fields` array.
- `opportunity.deleted`

**Our subscription:** `opportunity.updated` only. Server-side filter (configured on the subscription) narrows to status changes; the AEGIS handler additionally re-checks `data.status_id == <Docs In — Pre-UW status id>` and `"status_id" in event.changed_fields` before acting. Belt + suspenders.

**Close also emits these Lead events** (informational; we do NOT subscribe to them in v1):
- `lead.created`, `lead.updated`, `lead.deleted`, `lead.merged`
- `lead.updated` includes custom field changes (no separate "custom field changed" event)

**Custom Activity events** (deferred — not subscribed in v1):
- `activity.custom_activity.created`, `.updated`, `.deleted`
- `activity.opportunity_status_change` is ALSO emitted on status changes — duplicate-ish coverage. We pick `opportunity.updated` because it carries the `previous_data` and `changed_fields` arrays needed for the server-side filter.

**Delivery semantics worth designing for:**
- Close retries failed deliveries with exponential backoff up to every 20 minutes, for 72 hours.
- Subscription auto-pauses at 100k backlogged events or 3 days of failed delivery.
- Event ordering is NOT guaranteed (event consolidation + parallel retries).
- Async processing recommended. For v1 we handle synchronously in the route; refactor to arq if call volume warrants. The current synchronous /upload + /score work is the precedent.
- The Event Log API retains 30 days of history for recovery.

---

## Webhook security (explicit)

The `/webhooks/close` handler MUST implement, no exceptions:

- **Signature header:** `close-sig-hash` (Close's standard, NOT `X-Close-Signature`)
- **Timestamp header:** `close-sig-timestamp`
- **Algorithm:** HMAC-SHA256 of `close-sig-timestamp + raw_request_body` (concatenated, no separator) using `CLOSE_WEBHOOK_SECRET` (returned hex-encoded by Close when the subscription was POSTed)
- **Compare:** constant-time via `hmac.compare_digest(presented_hex, expected_hex)`. Never `==`.
- **Freshness:** reject if `close-sig-timestamp` is more than **5 minutes** old or in the future — same window as `src/aegis/api/routes/funder_replies.py` and `webhooks_zoho.py`.
- **Failure modes:** return **401** on bad signature OR stale timestamp. Body: `{"detail": "unauthorized"}` (generic — don't leak which check failed).
- **Secret handling:** `CLOSE_WEBHOOK_SECRET` loaded via `aegis.config.get_settings()`. Never logged — not in tracebacks, not in audit details, not in error responses. Logger masks by key name.
- **No body parsing before verification.** Read raw bytes first, verify, then `json.loads`.

Reference implementation to copy: `src/aegis/api/routes/funder_replies.py` (the funder-reply webhook). Same shape, different header names + signature construction.

---

## Migration safety

Pre-cutover probe (`scripts/db_checks/merchants-zoho-id-residue.sql`, run against prod 2026-05-20):

```
zoho_deal_id_count = 1
zoho_lead_id_count = 0
either_set         = 1
total_merchants    = 1
```

One merchant row has `zoho_deal_id` populated. Per operator rule, that's a non-zero-residue case → **RENAME, do not DROP**. Migration 026 renames the columns to `zoho_deal_id_archived` / `zoho_lead_id_archived` for forensic preservation. A future migration can drop them once the operator's certain no audit query needs them.

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
| `FICO Range` | `credit_score` | choice → int | Baked: <550→549, 550-599→550, 600-649→600, 650-699→650, 700+→700 |
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

### DB schema change (RENAME, not DROP)

```sql
-- migrations/026_close_lead_id.sql
ALTER TABLE merchants ADD COLUMN close_lead_id TEXT UNIQUE;
CREATE INDEX idx_merchants_close_lead_id ON merchants (close_lead_id)
  WHERE close_lead_id IS NOT NULL;

-- Preserve, don't drop — 1 prod row has zoho_deal_id set (verified
-- 2026-05-20 via scripts/db_checks/merchants-zoho-id-residue.sql).
ALTER TABLE merchants RENAME COLUMN zoho_deal_id TO zoho_deal_id_archived;
ALTER TABLE merchants RENAME COLUMN zoho_lead_id TO zoho_lead_id_archived;

-- Indexes on the old columns are preserved automatically by Postgres
-- on a RENAME COLUMN. A future migration drops both columns + indexes.
```

---

## Inbound payload shape (from Close)

Close POSTs to our endpoint with this top-level body:

```json
{
  "event": {
    "id": "ev_...",
    "date_created": "2026-05-20T...",
    "action": "updated",
    "object_type": "opportunity",
    "object_id": "oppo_...",
    "lead_id": "lead_...",
    "organization_id": "orga_...",
    "user_id": "user_...",
    "changed_fields": ["status_id", "status_label", "date_status_changed"],
    "previous_data": { "status_id": "stat_...", ... },
    "data": { "status_id": "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI", ... },
    "meta": {},
    "request_id": "req_..."
  },
  "subscription_id": "whsub_..."
}
```

Handler logic:
1. Read raw body bytes (NOT json) → verify HMAC + freshness → 401 on fail.
2. `json.loads` the body. Reject malformed → 400.
3. Filter: `event.action == "updated"` AND `event.object_type == "opportunity"` AND `"status_id" in event.changed_fields` AND `event.data["status_id"] == DOCS_IN_PRE_UW_STATUS_ID`. If any miss, return 204 (ack but no-op — acceptable to Close).
4. `lead_id = event.lead_id` → `client.get_lead(lead_id)` → upsert `merchants` row keyed by `close_lead_id`.
5. Audit row: `close.opportunity.underwriting_triggered` with `{lead_id, opp_id, previous_status, new_status}`.
6. Return 204.

---

## Implementation steps

Each step is its own commit on `feature/close-integration`. The branch is not deployable until all 12 land.

1. **Migration** — `migrations/026_close_lead_id.sql`. Add `close_lead_id` UNIQUE; RENAME `zoho_deal_id` → `zoho_deal_id_archived` and `zoho_lead_id` → `zoho_lead_id_archived`. Probe in `scripts/apply_migrations.py`.
2. **Close client** — `src/aegis/close/client.py`. API-key auth (HTTP Basic, key as username, blank password — Close's standard pattern). Retrying GET/PATCH/POST via httpx + tenacity (3 attempts, exponential backoff on 429/5xx). Methods: `get_lead`, `update_lead_custom_fields`, `get_opportunity`, `download_attachment`.
3. **Field map** — `src/aegis/close/field_map.py`. Pure functions: Close payload → AEGIS row, AEGIS values → Close payload. FICO Range parser (lower-bound conservative, baked), Industry → NAICS lookup, Entity type dedup, money-text → Decimal.
4. **Inbound webhook** — `src/aegis/api/routes/webhooks_close.py`. `POST /webhooks/close`. HMAC-SHA256 of `close-sig-timestamp + raw_body` using `CLOSE_WEBHOOK_SECRET`. 5-minute freshness. 401 on bad sig or stale timestamp. Filter for Opportunity status change to `Docs In — Pre-UW`. Pull Lead via client, upsert `merchants` keyed by `close_lead_id`.
5. **Outbound write-back** — `src/aegis/close/sync.py`. `push_decision_to_close(close_lead_id, decision, ofac_status)`. One PATCH. Audit row: `close.lead.decision_pushed`.
6. **Operator-triggered sync route** — `src/aegis/api/routes/deals.py`. Replace `/deals/{merchant_id}/sync-to-zoho` with `/deals/{merchant_id}/sync-to-close`.
7. **Hybrid statement path** — `src/aegis/api/routes/upload.py` add optional `close_lead_id` query param. Add `POST /uploads/from-close` that fetches via `CloseClient.download_attachment()`. Both paths converge on `documents.id`.
8. **Zoho deletion (hard cutover)** — delete `src/aegis/zoho/`, `src/aegis/api/routes/webhooks_zoho.py`, `tests/test_zoho.py`, all Zoho helper usages, `ZOHO_*` env vars from `.env.example` and `deploy/aegis.env.example`. DB columns are renamed (step 1), not dropped — code references to them are removed.
9. **Config** — `src/aegis/config.py`: add `CLOSE_API_KEY`, `CLOSE_WEBHOOK_SECRET`, `CLOSE_API_BASE` (default `https://api.close.com`). Boot warning if any `ZOHO_*` env var lingers.
10. **Tests** — `tests/test_close_client.py`, `tests/test_close_field_map.py`, `tests/test_close_sync.py`, `tests/api/test_webhooks_close.py`. Target ≥20 new tests including: HMAC verification (good + bad sig), stale timestamp rejection, status-change filter (correct stage / wrong stage / no status change → all return 204 without action), Lead upsert idempotency, FICO Range parser edge cases, Entity type dedup, decision push-back idempotency, audit-write-failure-raises.
11. **Documentation + memory** — update `CLAUDE.md` Tech Stack (Zoho → Close); add memory `feedback-aegis-close-intake.md` superseding `feedback-aegis-zoho-intake.md`.
12. **Deploy** — push branch, await merge approval, then standard chain: local merge → push main → deploy → smoke check.

---

## Reused patterns (don't reinvent)

- **HMAC + freshness webhook verification** — copy `funder_replies.py` shape (HMAC compute order, freshness compare, 401 on fail, no log of secret). Differ on header names + signature construction (Close uses `close-sig-hash` over `timestamp + body`).
- **HTTP client with retry** — model after `src/aegis/zoho/client.py` but simpler (API key, no token refresh). Tenacity retry: 3 attempts, exponential backoff on 429/5xx.
- **Decimal-as-string serialization** — mirror `src/aegis/zoho/sync.py` for money fields. `str(Decimal(...))`, never `float`.
- **Audit-write-failure-raises** — match `src/aegis/audit.py` contract. Failed `audit.record()` propagates and fails the operation.
- **Idempotent upsert keyed by external ID** — model after `ZohoSync.apply_inbound()` in `src/aegis/zoho/sync.py`, key on `close_lead_id` instead.

---

## Verification

1. **Unit tests** — `mypy src tests scripts` clean; `ruff check src tests scripts` clean; `pytest -q` shows ≥20 new Close tests and no regressions vs current main (~2172 passing).
2. **Migration dry-run** — `python scripts/apply_migrations.py --target prod --dry-run` shows exactly one pending migration: `026_close_lead_id.sql`. Real apply gated on operator approval.
3. **Inbound webhook smoke** — bad signature → 401; stale timestamp (>5 min) → 401; signed payload for non-trigger status → 204 with no `merchants` write; signed payload for Docs In — Pre-UW status change → 204 + `merchants` row created with `close_lead_id` set + audit row landed.
4. **Outbound smoke** — manually trigger `/deals/{merchant_id}/sync-to-close` against a test Lead in Close, verify the 4 Aegis-* fields populate and `Aegis Last Synced` is recent. Audit row lands in `audit_log`.
5. **Worker startup line** — unchanged: `parse_document, process_funder_reply, cron:run_archive_cron`. No new worker job in this branch.
6. **No Zoho code references** — `grep -ri "zoho" src/ tests/` returns nothing (DB columns `*_archived` still exist; not referenced from code).

---

## Out of scope (explicitly deferred)

- **Submission / Offer / Decline custom activities.** Custom Activity Types exist in Close (`actitype_*`), but writing them is deferred. The operator currently sees funder activity inside AEGIS; Close timeline integration is a follow-up.
- **Pipeline stage automation.** AEGIS will not transition Opportunity statuses. The operator drives Discovery → Underwriting → Lender Shopping → ... manually.
- **Zoho-historical data migration.** The "Zoho Pipeline" mirror in Close already exists (operator-managed). This branch does not touch historical Zoho leads; they live where they live. The one merchant row with `zoho_deal_id` is preserved (renamed column) for forensic lookup.
- **Per-Opportunity scoring history.** AEGIS fields are Lead-level. Renewals overwrite. Per-deal history will come when custom activities are added.
- **Bulk reconciliation tool.** If Close and AEGIS diverge (operator edits in Close, AEGIS doesn't see it until next webhook), there's no batch reconcile job. Out of scope.
- **Async worker for `/webhooks/close`.** Synchronous handler in v1. Refactor to arq enqueue if call volume warrants. Close's 72-hour retry window provides backpressure tolerance until then.
- **Drop of `zoho_deal_id_archived` / `zoho_lead_id_archived`.** Future migration when operator certifies no audit query needs them.
