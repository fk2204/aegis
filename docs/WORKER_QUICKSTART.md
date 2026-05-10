# AEGIS — Worker Quickstart

**Audience:** Anyone Commera Capital adds to the AEGIS dashboard.
**Time to read:** 8 minutes.
**Time to first useful action:** 15 minutes.

AEGIS is the underwriting brain. You drop bank statements in, it returns a
score, flags fraud signals, and matches the deal to funders. Your job as
a worker is to **intake the deal, review the findings, and decide
yes/no/needs-more-info**.

---

## 1. Logging in

1. Open https://aegis.commerafunding.com (or whatever URL your manager gave you).
2. Cloudflare Access shows a "Sign in" screen. Pick the email method you
   were invited with.
3. Check your inbox for a one-time PIN. Paste it in. You're in.
4. You land on the AEGIS index. The top bar has seven links — left to
   right: home, **Intake**, **Upload**, **Deals**, **Merchants**,
   **Funders**, **Review**.

If you get a "401" or "access denied" page, your email isn't in the access
policy yet — message the operator.

---

## 2. Your daily flow

```
  ┌─────────┐    ┌─────────┐    ┌──────────────┐    ┌──────────┐
  │ Intake  │ -> │ Upload  │ -> │ Findings     │ -> │ Match    │
  │ deal    │    │ N PDFs  │    │ panel        │    │ funders  │
  └─────────┘    └─────────┘    └──────────────┘    └──────────┘
       1              2                3                 4
```

### Step 1 — Intake (one screen, one submit)

Use the **Intake** page when a new merchant comes in. Fill the 5 required
fields plus whatever else you have:

| Required | Optional |
|---|---|
| Business name | DBA, NAICS, credit score, time in business |
| Owner name | Email, phone |
| State (2-letter) | **EIN** (PII — masked in exports) |
|   | Entity type (LLC/Corp/Sole prop/Partnership) |
|   | Requested advance / factor / term days |
|   | Broker source, intake date |
|   | Renewal flag |

Drop **3-4 months of bank statements** into the file picker at the bottom.
PDFs only, 25 MB per file, 100 MB total. Click **Create merchant & queue
parse**.

You'll land on the merchant detail page (the **Findings Panel**). The
documents you just uploaded show as `pending` for ~30-60 seconds while
the worker parses them with Claude. Refresh to see updates.

### Step 2 — Upload (existing merchant)

Use **Upload** when the merchant is already in the system. Pick the
merchant from the dropdown, drop their new statement(s), submit.

**You don't have to pick a merchant** — orphan uploads work for ad-hoc
batches, but you should link them to a merchant from the Merchants page
afterward.

### Step 3 — Findings Panel (the most important screen)

Open any merchant detail page (`/ui/merchants/<id>`). You'll see in order:

1. **Header ribbon** — state tier (1/2/3), OFAC check, renewal flag,
   broker source, intake date. Red pill on the state = unserved (we don't
   touch this state).
2. **Documents table** — every statement uploaded for this merchant. The
   `parse_status` column matters:
   - `proceed` 🟢 — clean, math reconciles, no fraud signals
   - `review` 🟡 — soft concerns (2 EOF markers / minor pattern flag).
     Aggregates still computed. Worker decides whether to ship.
   - `manual_review` 🔴 — math doesn't reconcile or hard tampering
     signal. The aggregates may be wrong. **Do not ship without
     verifying numbers against the printed PDF totals manually.**
   - `pending` ⚪ — worker hasn't picked it up yet. Wait 60s and refresh.
   - `error` ⚫ — extraction broke. Message the operator.
3. **Aggregate tiles** (latest statement) — True Revenue, Average Daily
   Balance, NSF count, days negative, MCA daily total. Each has a **drill
   down** button that lists the exact transactions feeding that number.
4. **Score breakdown** — Tier A/B/C/D/F and per-factor delta table. If
   recommendation is **decline**, you'll see the hard-decline reasons
   listed above.
5. **Stacking card** (only shows when MCA debits detected) — daily MCA
   burden + monthly projection (×22 business days) + expandable list of
   contributing debit rows.
6. **Pattern flags** — `[META]`, `[MATH]`, `[PATTERN]` codes. Each one
   tells you what fraud signal fired.

### Step 4 — Match Funders

Click **Match Funders** in the header to see which active funders this
deal qualifies for. Cards are color-coded:

- **Green** — match score > 0, zero soft concerns. Ship to this funder.
- **Yellow** — match score > 0 with soft concerns. Ship but flag the
  concern in the cover note.
- **Red** — at least one hard fail. Don't ship.

To pick a preferred funder right now, use the bearer API (the UI button
is deferred until we get usage signal). Ask the operator how.

---

## 3. Reading the score breakdown

The **Tier** maps to factor rate + holdback:

| Tier | Factor | Holdback | Typical use |
|------|--------|----------|-------------|
| A    | 1.18   | 10%      | Strong revenue, no NSF, no MCA |
| B    | 1.29   | 12%      | Good revenue, ≤1 NSF, no stacking |
| C    | 1.35   | 15%      | Moderate revenue, 2-4 NSF, stable |
| D    | 1.45   | 20%      | Thin margins, 5-9 NSF, one MCA |
| F    | —      | —        | Hard decline (decline reason listed) |

The **Per-factor delta** shows what pushed the score up/down. Green
numbers add to the score, red subtract. If you disagree with a factor's
weight, message the operator — these are tuned and shouldn't be
overridden ad-hoc.

---

## 4. Exporting findings

On the merchant detail page, top of the header there's a **Download CSV**
link. The CSV contains every section you see on screen (merchant header,
compliance ribbon, document analyses, score breakdown, stacking summary)
flattened into rows. Open in Excel.

**EIN is never in the CSV.** It's PII — kept in the database for our
records, but excluded from every export. Don't email screenshots of the
edit form if EIN is visible.

---

## 5. Common situations

### "All my uploads are stuck on `manual_review`"

Most common cause: the PDFs have 3+ `%%EOF` markers (incremental save
tampering signal). Real tampering looks like this. False positives are
rare since we raised the threshold (May 2026 fix). Check the pattern
flags — `[META] incremental_saves: 3 EOF markers` confirms it. If you
trust the source, the operator can re-parse manually.

### "The math reconciliation flag fired but I want to ship it"

The parser refused to ship aggregates it couldn't tie out to printed
totals. This is by design. Either:
- The PDF is genuinely garbled (re-request from the merchant)
- The parser missed a transaction format we haven't trained on (operator
  files a parser bug)

Don't override. Don't paste numbers from the PDF into a manual form. The
audit trail dies if you do.

### "A merchant has 3 statements but only the latest shows aggregates"

The Findings Panel shows **all documents in the table** but tiles +
score breakdown reflect only the **latest** statement. To compare months
side-by-side, download the CSV — each statement gets its own row in the
`document` section.

### "Cloudflare locked me out"

Either your invitation expired or your IP doesn't match the access
policy. Message the operator with: your email, the timestamp, and a
screenshot of the Cloudflare denial page.

---

## 6. What NOT to do

- ❌ **Don't paste credentials or merchant data into ChatGPT** or any
  external LLM. AEGIS uses Claude via AWS Bedrock with US-only routing
  to keep bank data inside the US. Outside tools break that promise.
- ❌ **Don't share the bearer token** (for the API). Use the dashboard
  via SSO. Only the operator handles tokens.
- ❌ **Don't email PDFs** to/from your personal address. Use the upload
  flow. Logs and audit trail rely on every upload going through AEGIS.
- ❌ **Don't ship a `manual_review` deal** without operator sign-off.
- ❌ **Don't add a state to the served list** — that's a legal
  decision (compliance citations required). Ask the operator first.

---

## 7. Asking for help

| Problem | Who | How |
|---|---|---|
| Login broken | Operator | Email with screenshot |
| Parsed numbers look wrong | Operator | Screenshot of findings panel + filename |
| Match is empty / wrong | Operator | Merchant ID + the question |
| Bug in the UI | Operator | Filename + what you clicked + what happened |
| State / disclosure compliance | Operator (legal) | Don't decide alone |

---

## 8. Pages reference card

| Path | What it does |
|---|---|
| `/ui/` | Home — summary tiles |
| `/ui/intake` | One-screen merchant + N statements (start here for new deals) |
| `/ui/upload` | Drop N statements into an existing merchant |
| `/ui/deals` | All merchants joined with their latest document |
| `/ui/merchants` | All merchants (table) |
| `/ui/merchants/{id}` | **The Findings Panel** — your main workspace |
| `/ui/merchants/{id}/edit` | Edit merchant intake fields |
| `/ui/merchants/{id}/match` | Funder match cards (green/yellow/red) |
| `/ui/merchants/{id}/findings.csv` | Export everything as CSV |
| `/ui/funders` | All active funders |
| `/ui/funders/import` | Upload a funder-criteria PDF, review extraction, save |
| `/ui/review` | Documents in `manual_review` state — operator queue |

---

**Version:** 1.0 (2026-05-10)
**Source:** `docs/WORKER_QUICKSTART.md` — edit there, never share PDF copies.
