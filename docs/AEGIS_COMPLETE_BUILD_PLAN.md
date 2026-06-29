# AEGIS Complete Build Plan

Everything queued. All 70 items across 12 sections. Phased for execution.

**Live as of 2026-06-29 evening.** 10-funder catalog (post-cleanup),
migration 099 §1071 columns applied, dashboard parallelized (asyncio.gather
+ to_thread, ~5s warm), local-folder funder sync (`scripts/sync_funders_from_folder.py`),
§1071 dossier collection panel shipping for loan / line-of-credit products.

**Status as of 2026-06-28 (historical):** Phase 1 in flight. Sections 2-12 queued.

---

## SECTION 1 — IN FLIGHT (finish before anything new)

### 1.1 — Migration 089: documents indexes (approved, waiting apply)

```sql
CREATE INDEX IF NOT EXISTS idx_documents_merchant_uploaded
  ON documents(merchant_id, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_parse_status
  ON documents(parse_status)
  WHERE parse_status IN ('proceed','manual_review','pending');
```

Apply to prod. Then slim `list_documents` hot path — separate `all_flags`
into `get_document_flags(document_ids)` batch fetch. Target: `/ui/` warm
under 2s.

### 1.2 — Remove OpenAI completely

No OpenAI anywhere. Remove from `pyproject.toml`, all config files, all
env examples, the box `/etc/aegis/aegis.env`. AEGIS uses Bedrock only.

### 1.3 — Close full re-sync for all 59 leads

28 merchants updated so far. 31 more with no FINANCIAL block. Re-sync
all 59 — for the ones with no structured FINANCIAL block, parse whatever
is in the description field. Transplex, Edgren, B&A Towing, Fullerworks
all have rich data in Close that isn't in AEGIS yet.

### 1.4 — Verify narrator backfill (33 jobs enqueued)

Check if narrator summaries generated on proceed merchants. If jobs
failed, fix the root cause and re-enqueue.

---

## SECTION 2 — PERFORMANCE (target: every page under 2s)

### 2.1 — Dashboard warm: 4.5s → under 2s

Remaining bottlenecks after the 2026-06-28 perf night:
- `_build_attention_groups`: still does per-merchant analyses fetches in some paths
- `build_shadow_review_attention_section`: 5-min Redis cache helps warm, but cold is 8.7s
- `count_by_parse_status`: migration 089 companion RPC function will help

### 2.2 — Dossier load: target under 1s

The dossier now loads background checks from cache (24h TTL). Remaining
slow path: if cache is empty (first load on a new merchant), OFAC +
bankruptcy + SOS all run synchronously. Fix: for first-load merchants,
show "Checking…" spinner and use BackgroundTasks to run checks after
page renders.

### 2.3 — Merchant list: target under 500ms

Currently ~1s. The list query loads too many columns. Apply same
slim-columns pattern as documents.

### 2.4 — Pipeline view: 3.9s → under 1s

Still calling `score_deal()` in some paths. Find and remove all
remaining `score_deal()` calls from list views.

---

## SECTION 3 — UI & USABILITY

### 3.1 — Dashboard layout: fill the screen

The key-numbers banner and monthly strip render correctly now but the
today-cols 3-column grid (38/38/24) leaves vertical gaps. The attention
queue and pipeline columns should fill their full height. Fix with
`align-items: stretch` instead of `start`.

### 3.2 — Dossier: application data section visible

Migration 087 added the § 4⅔ Application data section with amber border.
Verify it's rendering for Rendezvous (9 fields confirmed populated).
Take a screenshot to confirm.

### 3.3 — Dossier: 6-month revenue trend per merchant

The monthly strip on the dashboard shows aggregate data. The dossier
needs a per-merchant 6-month revenue trend from `analyses.monthly_breakdown`.
Show: month label, true revenue, NSF count, ADB, trend arrow vs prior month.

### 3.4 — Merchant list: fix NAICS 999999 display

The `b7d5137` commit added NAICS lookup but Rendezvous still shows
"999999" in the merchant list (the fix only landed in the dossier
route, not the list route). Apply the same NAICS lookup to the
merchant list template.

### 3.5 — Pipeline view: add estimated advance amount

"Ready to Submit" cards should show the offer sizing from
`offer_recommendation` — estimated advance, factor rate, estimated term.
Operators need this to know what they're submitting.

### 3.6 — Statement upload: product-aware document guidance

When uploading a statement, show what documents are needed based on `product_type`:
- Revenue-based / MCA → bank statements only
- Business loan → bank statements + 2yr tax returns + P&L
- Equipment → equipment quote/invoice + bank statements
- LOC → bank statements + tax returns
- ABL → A/R aging report + bank statements
- Factoring → invoice copies + A/R aging + bank statements

### 3.7 — Notification bell: confirm red badge working

327 unread notifications exist. Verify the red badge is visible. If
not showing, the conditional rendering may be checking the wrong field.

### 3.8 — BUG-E shadow detector: operator validation UI

Build a simple 10-item review screen at `/ui/admin/text-layer-probe-review`.
Shows documents where v2 probe disagrees with v1. Operator clicks
"v2 correct" or "v1 correct". After 10 confirmations where v2 is
correct → auto-flip the gate from shadow to live.

---

## SECTION 4 — SCORING & INTELLIGENCE

### 4.1 — Track B on all proceed merchants

Verified: 3 proceed docs have 67-84 transactions. Track B should now
compute on these. Verify paper grades are showing (A/B/C/D/F) and
offer sizing is populated.

### 4.2 — Funder matching: verify BUG-B fix worked

NY §600.21(f) hard-fail removed (commit `c232577`). Open Rendezvous,
Transplex, B&A Towing dossiers and confirm funder matches now appear.
If still empty, run the diagnostic query again.

### 4.3 — Impossible payment detector: wire to real leads

`detect_impossible_payment_load` built but needs `stated_daily_payment`
from Close. Transplex has $4,764.35 daily payment on $175K monthly
revenue = $104K/month implied. Transplex's payment load is 59% of
stated revenue — high but not impossible. Vibration Guys was the
original trigger case. Verify the detector runs correctly on Transplex.

### 4.4 — Stated vs measured revenue divergence: test with real data

Fullerworks submitted Monthly Gross Revenue: 55 (data entry error —
likely $55,000). AEGIS has this stored as $55. If bank statements show
something different, the divergence detector should fire. Verify.

### 4.5 — Paper grade calibration

The paper grade (A/B/C/D/F) uses scoring thresholds calibrated for MCA.
Now that we have 6 products, calibrate thresholds per product type:
- Revenue-based: current thresholds
- Business loan: DSCR weight higher, stacking weight lower
- Equipment: asset coverage ratio replaces stacking
- LOC: revolving utilization pattern
- ABL: A/R quality score
- Factoring: invoice aging distribution

### 4.6 — Flip shadow detectors to live

- `unreconciled_internal_transfer_v2` — been in shadow for weeks. Pull
  signal log, check false positive rate, flip if < 5% FPR.
- `ai_generated_statement` — same process.

---

## SECTION 5 — VERIFICATION STACK

### 5.1 — SOS WY via Playwright (Commera home state)

`wyobiz.wy.gov` requires JS session. Build a Playwright-based scraper
specifically for WY. ~150K entities. Critical because Commera is
Wyoming-registered.

### 5.2 — SOS FL via Sunbiz HTTP

Florida is the highest-volume state for MCA deals. Sunbiz FTP blocked
on Hetzner. Find the HTTP bulk download URL. ~3.5M entities. If HTTP
also blocked, submit a formal data request to FL DOS — they provide
bulk files on request.

### 5.3 — SOS remaining states: NY, OH, MN, IA

- NY: ~5M entities, no accessible entity master dataset found. Try
  `data.ny.gov` search for alternative datasets.
- OH: 403 on bulk files. Try with User-Agent header spoofing or
  contact OH SOS data team.
- MN: JS session required. Build Playwright scraper like WY.
- IA: `data.iowa.gov` catalog 404. Find new dataset ID.

### 5.4 — OFAC consolidated non-SDN: verify complete coverage

CONSOLIDATED.XML now loading via `8675881` fix. Run `update_ofac_list.py`
and verify entity count exceeds 38,605. The consolidated list should
add ~15,000 more entities.

### 5.5 — BUG-E: vision routing for image-only PDFs

After operator validates 10 disagreements and confirms v2 is correct
→ flip `text_layer_probe_v2` from shadow to live. Then re-parse
Arthur State Bank, PNC Bank, Bank of Bennington documents via vision mode.

### 5.6 — Trade licensing: expand portal map

Currently covers FL/TX/CA/GA/NY/NC/CO/OH/AZ/WA/IL/NJ/PA/MI/MA/VA/WY.
Add remaining states with major licensed trade activity: NV, UT, OR,
WA, MN, SC, TN, MO. Also add: auto dealer licensing (TX, FL), trucking
authority (FMCSA), healthcare facility licensing.

---

## SECTION 6 — DOCUMENT PARSING

### 6.1 — Tax return parser (Phase F) — critical for loans/LOC

Looking at Close leads: Rendezvous (s-corp → 1120-S), Transplex (LLC
→ 1065 or Schedule C), Edgren Investments (equipment → need returns
for loan approval). Build Bedrock vision extraction for:
- Form 1120 (C-corp): gross receipts, net income, total assets, total liabilities, officer compensation
- Form 1120-S (S-corp): gross receipts, ordinary business income, shareholder W-2
- Form 1065 (partnership): gross receipts, net income, partner distributions
- Schedule C (sole prop): gross receipts, net profit/loss, COGS
- 1040 personal: AGI, total income, itemized deductions

Store on `tax_returns` table (separate from merchants — one merchant
can have multiple years). Surface on dossier as "Tax Return Summary"
with YoY comparison.

### 6.2 — A/R aging parser

Transplex is a medical rehab center with $200K/month CC sales —
perfect factoring/ABL candidate. Build parser for:
- Excel `.xlsx` A/R aging exports (most accounting software)
- CSV A/R aging
- PDF A/R aging (Bedrock vision)

Extract: total outstanding, current, 30/60/90/120+ day buckets, top 10
debtors, concentration percentage.

### 6.3 — Equipment invoice/quote parser

B&A Towing wants equipment purchase ($52.5K requested). Edgren
Investments wants equipment ($200K). Build parser for equipment
quotes/invoices — extract: equipment description, make/model/year,
condition (new/used), vendor, total cost, VIN/serial if present.

### 6.4 — Processor statement parser: complete coverage

Stripe, Square, Toast, Clover parsers exist but data isn't consistently
flowing into scoring. Verify each processor parser actually writes
transaction rows to the `transactions` table and that Track B picks
them up.

### 6.5 — Bank corpus ingestion pipeline

You have a folder of real bank statements for training. Build the
isolated ingestion pipeline:
- Migration 090: `corpus_documents` table (no FK to live pipeline)
- `scripts/ingest_training_corpus.py`: SHA-256 dedup, extract metadata,
  detect bank, run forensic checks, write to `bank_layout_hints` and
  `creator_fingerprint_registry` for clean statements
- Never creates merchants, never touches live pipeline tables

Migration 090 SQL (report before applying):

```sql
CREATE TABLE IF NOT EXISTS corpus_documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_hash       text NOT NULL UNIQUE,
    original_path   text,
    bank_name       text,
    detected_creator text,
    detected_producer text,
    page_count      integer,
    has_font_inconsistency boolean DEFAULT false,
    has_text_overlay boolean DEFAULT false,
    has_creator_mismatch boolean DEFAULT false,
    fraud_signals_fired boolean DEFAULT false,
    ingested_at     timestamptz DEFAULT now(),
    notes           text
);
CREATE INDEX idx_corpus_bank ON corpus_documents(bank_name);
CREATE INDEX idx_corpus_hash ON corpus_documents(file_hash);
COMMENT ON TABLE corpus_documents IS
  'Training corpus — isolated from live pipeline. No FK to merchants/documents.';
```

### 6.6 — Creator fingerprint registry: seed with real data

After corpus ingestion, auto-populate the registry with real
Creator/Producer strings from clean bank statements. Fixes GAP-05 —
currently has placeholder Adobe values.

---

## SECTION 7 — CLOSE CRM INTEGRATION

### 7.1 — Webhook: auto-sync application data on lead update

Currently the Close webhook fires on new attachments (PDF upload).
Extend it to also fire when the lead description changes — so when
a rep updates an application field in Close, AEGIS automatically
re-parses and updates the merchant's `stated_*` fields.

### 7.2 — Webhook: auto-create merchant on qualified lead

When a Close lead status changes to "Qualified – Opp Open",
automatically create an AEGIS merchant record if one doesn't exist.
Currently operators have to manually create merchants in AEGIS.

### 7.3 — Close task creation for compliance gates

When AEGIS triggers a compliance gate (licensing verification needed,
OFAC block, bankruptcy block), automatically create a Close task
assigned to the deal owner with the specific action required.

### 7.4 — Close activity on funder submission

When an operator submits to a funder in AEGIS, automatically log a
Close activity note: "Submitted to [Funder] — advance $X, factor X.XX,
estimated term X months."

### 7.5 — Close activity on deal outcome

When an outcome is recorded in AEGIS (funded/declined/withdrawn),
automatically update the Close opportunity status and log an
activity note.

---

## SECTION 8 — FUNDER CATALOG & MATCHING

### 8.1 — Funder guidelines upload

Add "Upload guidelines PDF" button on each funder detail page. Extract
key criteria via Bedrock: min revenue, min FICO, min TIB, stacking
policy, excluded industries, excluded states, preferred products.
Store as structured JSON on `funders.guidelines_data`.

### 8.2 — Add 6 remaining major funders

Still missing from 27-funder catalog: Rapid Finance, Kapitus, Fora
Financial, Credibly, Expansion Capital Group, Forward Financing.

### 8.3 — Product-type funder routing accuracy

Verify `deal_types_accepted` is correctly set on all 27 funders.
Several funders added in batch seeding have empty or wrong product
tags. Equipment funders (Maxim Commercial Capital) should only match
equipment deals. Business loan funders (Idea Financial, World Business
Lenders) should only match loan deals.

### 8.4 — Marketplace funder integration

Big Think Capital, Bizi Connect, Splash Advance are tagged MARKETPLACE.
These funders accept submissions via a marketplace platform (Lendio,
Biz2Credit, etc.) rather than direct. Build a workflow for marketplace
submission: generate the standard marketplace application package
from AEGIS data, provide direct link to the marketplace submission form.

### 8.5 — Automated funder note generation

When a deal is ready to submit, auto-generate a funder note from
the dossier:
- Business summary (from narrator)
- Key financials (true revenue, ADB, NSF count, TIB)
- Stacking summary (positions, lenders, balance)
- Fraud assessment (Track A verdict)
- Offer request (amount, desired factor, term)

Operator reviews, edits if needed, submits. Saves 20-30 min per submission.

---

## SECTION 9 — LEARNING & CALIBRATION

### 9.1 — Deal outcomes: start recording immediately

The `deal_outcomes` table is empty. Every deal that closes (funded,
declined, withdrawn, expired) needs an outcome. Add a prominent
"Record Outcome" button to the dossier Disposition section — one
click, modal asks: Funded/Declined/Withdrawn, Funder, Amount Funded
(if funded), Decline Reason (if declined). This is the most important
thing for the system to learn.

### 9.2 — Override flywheel: connect to outcomes

When an override deal (operator overrode AEGIS recommendation) reaches
an outcome, connect the override record to the outcome. Track: how
often overrides lead to funded vs declined. Surface on the overrides
dashboard.

### 9.3 — Calibration engine: activate

`/ui/calibration` is live but empty. After recording 20+ outcomes,
the calibration engine has enough data to suggest threshold adjustments.
Schedule a monthly review: look at Track A/B/C verdicts vs actual
outcomes, adjust weights.

### 9.4 — Bank hints auto-improvement

Currently 14 new bank hints seeded with `successful_parses=0` —
they're armed but inert behind `HINTS_AVAILABLE_THRESHOLD=3`. After
each successful parse with a matching bank, increment the counter.
When a bank hits threshold=3, the hints become active for all future
parses of that bank.

---

## SECTION 10 — INFRASTRUCTURE

### 10.1 — Standby server

Single Hetzner CPX21. Add a second node in Helsinki (HEL) vs current
Ashburn (ASH). Daily snapshot sync. If ASH goes down, failover to HEL
takes 5 minutes with a DNS change. Cost: ~€15/month.

### 10.2 — Redis queue depth alerting

Cron every 5 minutes: if `arq:queue:default` > 20 jobs, send Slack
webhook alert. Add to `deploy/aegis-queue-monitor.timer`.

### 10.3 — Automated OFAC + SOS timer verification

`aegis-ofac-update.timer` and `aegis-sos-update.timer` are installed.
Verify they actually fire and succeed. Add a systemd `OnFailure` alert
that emails or Slack-messages when either timer fails.

### 10.4 — Database backup verification

Supabase has automated backups but they've never been tested. Run a
restore drill: restore to a test project, verify row counts match.
Schedule quarterly.

### 10.5 — SSL certificate auto-renewal

Cloudflare handles the public SSL. The internal Hetzner service uses
a self-signed cert. Verify it doesn't expire and set a calendar reminder.

### 10.6 — Log retention policy

`journalctl` logs grow unbounded. Set `MaxRetentionSec=30days` and
`MaxFileSec=1week` in `/etc/systemd/journald.conf`.

---

## SECTION 11 — COMPLIANCE

### 11.1 — State compliance audit: 40 remaining states

CA, FL, TX, NY, GA, NC, VA, CT, MO, UT audited. 40 states are Tier 3.
For a 6-product capital advisory firm doing business nationwide, this
gap grows with every product added. Prioritize: IL, PA, NJ, OH, MI,
WA, AZ, CO, WI, MN.

### 11.2 — TX OCCC deadline: surface in dashboard now

TX OCCC December 31, 2026 deadline (186 days away) not showing because
the attention window is 90 days. Change to 180 days.

### 11.3 — Trade licensing outcome tracking

When operator clicks "Mark license verified" it writes an override row.
Build a report at `/ui/compliance/licensing` showing: all deals where
licensing was required, which were verified, which were bypassed,
which are pending. Full audit trail.

### 11.4 — ReasonCode migration

`license_verified_manually` was added to the Literal only. Write
migration to add it to the Postgres CHECK constraint so the database
enforces it. **Migration 091**.

### 11.5 — §1071 data collection readiness

Business loan and line of credit products trigger §1071 small business
data collection requirements (CFPB rule). Track: race/ethnicity/sex
of principal owners, gross annual revenue, NAICS code, number of
workers, time in business, census tract. These fields need to be
collected at intake for loan/LOC products. Build intake form additions
for loan/LOC product types.

---

## SECTION 12 — PRODUCT EXPANSION

### 12.1 — SBA referral workflow

Rendezvous Inc: 35yr TIB, 708 FICO, $200K/month, s-corp, no bankruptcy
= textbook SBA 7(a) candidate. Build SBA eligibility detector:
- TIB ≥ 24 months: ✅
- FICO ≥ 650: ✅
- Not in excluded industries: check
- Revenue ≤ SBA size standard for NAICS: check
- No active bankruptcy: ✅

When eligible, show "SBA 7(a) Eligible" badge on dossier and generate
referral package.

### 12.2 — Equipment financing workflow

B&A Towing (equipment purchase, $52.5K) and Edgren Investments
(equipment, $200K) are both equipment candidates. Full workflow:
- Equipment document intake (quote/invoice parser from 6.3)
- LTV calculation (financed amount / equipment value)
- Equipment-specific funder matching (Maxim Commercial Capital,
  National Business Capital)
- Equipment-specific offer sizing (down payment %, monthly payment, term)
- Equipment-specific dossier section showing asset details

### 12.3 — Receivables financing workflow

Transplex ($175K/month, medical, $200K CC sales, TD Bank) is a strong
factoring candidate. Medical receivables factor at 70-85% advance
rates. Full workflow:
- A/R aging parser (from 6.2)
- Debtor concentration analysis
- Invoice quality scoring
- Factoring-specific funder matching
- Offer sizing: advance rate × eligible A/R balance

### 12.4 — Merchant portal enhancement

The pre-qualification form at `/apply` is live and generating leads
(3 test submissions this morning). The post-submission experience needs:
- Confirmation email with next steps
- Application status page showing where they are in the process
- Document upload portal (merchant can upload their own bank statements
  instead of email attachments)
- SMS status updates at key milestones

### 12.5 — Multi-product deal comparison

For a merchant that qualifies for multiple products (e.g. both MCA and
business loan), show a side-by-side comparison:

|   | MCA | Business Loan |
|---|---|---|
| Advance | $75K | $100K |
| Cost | $112.5K total | $115K total |
| Term | 6 months | 24 months |
| Daily payment | $625 | $208 |
| Funder | Credibly | Idea Financial |

Let operator choose the best fit before submitting.

---

## EXECUTION ORDER

- **PHASE 1 (tonight — finish what's started):** 1.1, 1.2, 1.3, 1.4, 2.1, 2.2
- **PHASE 2 (this week — daily usability):** 3.1-3.8, 4.1-4.4, 5.4, 7.1, 7.2, 9.1
- **PHASE 3 (next week — product completeness):** 6.1, 6.2, 6.3, 6.5, 8.1-8.3, 8.5, 9.2, 10.1, 10.2, 12.1, 12.2
- **PHASE 4 (this month — full platform):** 5.1-5.3, 5.5-5.6, 6.4, 6.6, 7.3-7.5, 8.4, 9.3-9.4, 10.3-10.6, 11.1-11.5, 12.3-12.5

---

## COMPLETION STATUS — 2026-06-29

**Phase 1 (DONE):** migrations 089 + 090 applied, OpenAI removed, slim
`list_documents` shipped, dashboard layout — `today-cols` stretch.

**Phase 2 (DONE):**
- 3.1 today-cols stretch (`e538589`)
- 3.4 NAICS in merchant list (`4ef3111`)
- 3.5 pipeline offer sizing (`0664860`)
- 3.6 product-aware doc upload guidance (`fd56d6c`)
- 3.8 text-layer probe v2 review UI (`8bfb977`) — 0 verdicts so far
- 4.1-4.4 verification stack (OFAC / bankruptcy / SOS / UCC) — shipped pre-session
- 5.4 forensic shadow detectors — corpus accumulating (0 signals / 0 v2 disagreements)
- 7.1 Close webhook description auto-sync (`9d05637`)
- 7.2 Close webhook auto-create on Qualified-Opp-Open (`0ae7ba2`)
- 9.1 prominent Record Outcome button + modal (`750a8c3`)
- 10.2 Redis queue depth monitor (`d257ded` + `35972cb`) + dashboard banner (`b88fab5`)
- 10.6 journald 30-day retention (`bc40c50`)
- 11.2 compliance window 180 days (`40b0b8f`)

**Phase 3 (DONE this session):**
- 6.1 tax return parser (`830b080`)
- 6.2 A/R aging parser (`9555c5d`)
- 6.3 equipment invoice parser (`a7213ce`)
- 6.5 bank corpus ingestion pipeline (`4ae515b`)
- 8.1 funder guidelines Bedrock extractor (`7345fed`) + upload route (`03f91a6`)
- 8.2 funder catalog cleanup — DELETED 18 unauthorized funders 2026-06-29,
  10 remaining (Big Think, Bizi Connect, Highland Hill, Logic Advance,
  Shor, Splash, SwiftSource, TMRNOW, United Capital Source, Velocity)
- 8.5 funder note generator (`27b8337` + `4491eaf`)
- 9.2 override flywheel auto-link + summary (`fbd1454` + `11de13f`)
- 12.1 SBA eligibility detector + dossier badge (`504db75` + `f26e93e`)

**Phase 3 INTEGRATIONS (DONE this session):**
- INT 1 workers.parse_document routing (`eafb979`)
- INT 2 tax + A/R dossier sections (`b97b659`)
- INT 3 funder guidelines upload route + button (`03f91a6`)
- INT 4 override flywheel auto-link in outcome write path (`11de13f`)
- INT 5 funder note CTA + draft/submit/discard routes (`4491eaf`)

**Newly DONE 2026-06-29 evening:**
- 1.x dashboard performance — `asyncio.gather` + `to_thread` rewrite
  shipped (`710b9a4`). Sequential helper calls parallelized.
- 11.5 §1071 dossier collection panel — Pydantic mirror of the migration-
  099 columns + collapsible dossier section for `product_type` in
  (`business_loan`, `line_of_credit`). HTMX POST to
  `/ui/merchants/{id}/sec1071` writes the seven §1071 columns and
  stamps `sec1071_collected_at`.
- 3.x merchant-list last-activity display — server-rendered
  "today" / "yesterday" / "Nd ago" string replaces the inline
  template branching that collapsed to "1d" for same-week uploads.
- 8.x local-folder funder sync — `scripts/sync_funders_from_folder.py`
  + `deploy/aegis-funder-sync*` timer; ingest_funder runs daily over
  a watched folder of criteria PDFs.

**Open (still queued):**
- 5.1-5.3, 5.5-5.6 — funder reply ingestion / weekly forensic review /
  SBA referral workflow expansion.
- 6.4 FL SOS bulk download — Sunbiz returns 403 Forbidden, blocked
  upstream. Documented and parked.
- 6.5 corpus ingestion auto-seed from existing proceed docs — needs
  schema discovery (`analyses.pdf_metadata` doesn't exist; metadata
  sits in `documents.metadata_flags` strings).
- 7.3-7.5 Close task creation on compliance gates / outcome capture
  on funder reply / etc.
- 8.3 funder marketplace submission workflow.
- 9.3-9.4 calibration engine, hint auto-improvement.
- 10.1, 10.3-10.5 standby server (documented `f1ba8dd`), DB backup
  restore drill, SSL auto-renewal verification.
- 11.x compliance state audits (40 states remaining).
- 12.2-12.5 receivables financing, merchant portal, multi-product
  comparison.

**Held migrations (operator approval pending — already on `main` /
CI auto-applies on deploy):**
- 093 tax_returns
- 094 ar_aging_reports
- 095 merchants.equipment_details
- 096 funders.guidelines_data
- 097 corpus_documents
- 098 override_outcome_links
- 099 merchants §1071 fields (auto-included in `b88fab5`)

**Operational additions this session:**
- Google Drive funder sync (`2232907`) — daily 07:00 UTC timer; env
  vars `GOOGLE_DRIVE_CREDENTIALS_JSON` + `GOOGLE_DRIVE_FUNDERS_FOLDER_ID`
  must be set on the box before the timer is enabled.
- Standby server failover procedure documented (`f1ba8dd`).
- Funder catalog cleanup: 18 unauthorized funders deleted, 10
  operator-approved remain.

---

**Last updated:** 2026-06-29
