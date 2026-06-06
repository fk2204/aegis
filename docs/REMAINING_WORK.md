# AEGIS — Remaining work

**Snapshot taken:** 2026-06-06 (Step 2a shadow-comparison run; see new
"Step 2 of 3-track redesign" entry below).
**Purpose:** durable list of what's queued, parked, or systemic. So nothing's
lost between sessions.

Cross-references:
- `CLAUDE.md` — guardrails (binding rules learned from the work below)
- `docs/CLOSE_AUTOMATION_SPEC.md` — Option-A locked automation spec
- `docs/SCORING_REDESIGN_CONTINUATION.md` — 3-track scoring redesign
- `docs/audit-confirmed-bugs.md` — confirmed bugs awaiting decision-boundary fixes

---

## Immediate / queued builds

Ready to scope or already specced.

### "Add funder via Claude Code"
Hand Claude Code the PDF/PNG, it runs the existing extraction engine,
shows the fields in chat with low-confidence flagged, the operator
confirms, and Claude Code calls the existing `/ui/funders/new`
upsert path. **Thin wrapper, NOT autonomous** — confirmation
required (per CLAUDE.md "extraction assists, never replaces
judgment"). Reuses everything that already exists; no new write
path, no new prompt. Status: scoped, ready to build.

### Funder-extraction prompt — third rule
Add an explicit rule to `FUNDER_GUIDELINE_EXTRACTION_PROMPT` banning
AGENT-DUTY clauses (clawback windows, DFPI licensing requirements,
referral terms, "the agent must…" obligations) from any
merchant-gating bucket (`conditional_requirements`,
`auto_decline_conditions`). Route them to `notes_residual` instead.
Shor's ISO retest still leaked CA/VA agent-compliance clauses into
`conditional_requirements` at confidence 72 after the first
prompt-tighten. Non-blocking; iterate when convenient.

### `/uploads/from-close` route — xfail debt
Route at `src/aegis/api/routes/upload.py` calls
`CloseClient.download_attachment(attachment_id)` standalone without
priming the URL cache via `list_lead_attachments`. Now raises
`CloseError("cache miss")`. Was already broken in prod (same dead
endpoint as the listing). 5 tests xfail-marked in
`tests/api/test_uploads_from_close.py` with the pointer.

Fix: either prime the cache by calling `list_lead_attachments(close_lead_id)`
first, or accept a URL in the POST body and add a sibling
`download_attachment_by_url` method on the client. Low priority — no
live caller hits this route.

---

## Fraud-review list (human underwriting, NOT parser-salvage)

These are merchants AEGIS correctly gated to `manual_review` because the
parser flagged real integrity-signature concerns. They need a human
underwriter to look at the actual PDFs — not a parser fix, not a
threshold tweak.

| Merchant | Signature | Status |
|---|---|---|
| **VU Development 7722** (`5cf4479d…`) | Running-balance drift $5 / $11 / $55 across 3 of 4 months | Correctly gated by parser. Awaiting human review. |
| **A&R KM LLC** (`a522a8fb…`) | iText 2.1.7 editor metadata + reconciliation drift across 4 of 4 Lili Monthly Statements (period drift $15-$500, withdrawal-total drift $220-$690) | Correctly gated by parser. Awaiting human review. |

**Pattern watch:** two real merchants with the running-balance-drift
signature in the same week. If a third independent merchant arrives
with the same pattern, that suggests a bad source / channel /
referral relationship — worth a separate look at submission origin.

These are the real-world test cases for the integrity track of the
3-track scoring redesign — they prove the parser's tampering
detection works on real merchant data without any tuning.

---

## Parked — deliberate decisions for a fresh, focused session

Not ready to ship; need dedicated thinking, not opportunistic patching.

### 3-track scoring redesign — THE multi-week build
- **Spec:** `docs/SCORING_REDESIGN_CONTINUATION.md` (canonical) and
  `docs/AEGIS_SCORING_REDESIGN.md` (background).
- **Decided (Q1):** band → action mapping is
  `low = auto-forward`,
  `moderate/elevated = review-neutral`,
  `high = review-decline-default`.
- **Open (Q2):** integrity states —
  `fail = strong_metadata`,
  `review = medium_corroborated`,
  `clean = none`,
  AND running-balance-drift placement: drift alone = review;
  drift + editor metadata = fail.
  **VU 7722 and A&R KM are the test cases** — drift-alone vs
  drift+editor decides which way they'd auto-route.
- **Open (Q4):** own-account-unconfirmed — surface the gap, do NOT
  infer ownership.
- This subsumes A.2 (the 65/70 threshold) and the A.1 EOF gate as
  policy implementations within the new tracks.

### Step 2 of 3-track redesign — `fraud_score` retirement + A+B+C live cutover
- **Status:** A/B/C live ADDITIVE on the dossier (commit `5d53d5d`,
  verified VU + A&R KM 2026-06-05). The existing `score_deal` /
  `fraud_score` path still controls every production decision. Step 2
  retires `fraud_score` and flips A/B/C live. **NOT authorized** —
  gated on the conditions below.
- **Diagnostic:** `scripts/shadow_comparison_a_b_c_vs_fraud_score.py`
  (commit `973d7fd`). Read-only sweep — every merchant in the corpus,
  LIVE decision vs new A/B/C, categorised per disagreement bucket.
  Re-run periodically; treat exit code 3 (any
  `old-caught-something-new-misses` row) as a STOP.
- **Cutover gating conditions — ALL required, in order:**
  1. **Corpus growth.** The 2026-06-06 baseline is N=1 for Track B/C
     comparison (only VU had classified transactions; A&R KM had docs
     but the `manual_review` path persisted no analyses). A cutover
     decision on N=1 is not a population claim, it is a case
     observation. Need significantly more deals through both systems
     before "no regressions" means anything.
  2. **Track A historical lookback.** Pull every historical
     `fraud_score_critical` decline and verify Track A would have
     caught it (FAIL verdict) — explicitly proves Track A isn't
     missing the integrity signature `fraud_score` previously gated
     on. Document as `scripts/track_a_historical_lookback.py` output;
     corpus + box-run, not laptop-run.
  3. **Regression review.** Every
     `old-caught-something-new-misses` row across the accumulated
     re-runs gets per-merchant operator triage — categorise (genuine
     regression vs detector gap vs corpus-shape artifact) BEFORE
     flipping. Zero rows is not the bar; **reviewed rows** is the
     bar.
  4. **Deliberate flip.** When the gating conditions are met, the
     flip is an env var change (mirrors the tampering-rule pattern:
     shadow → live by config, not code deploy). The PR title says
     "retire fraud_score" — small, explicit, reversible.
- **What this is NOT:** a small-N green-light. The 2026-06-06 sweep
  landed VU=agreement and A&R KM=new-is-better; both were the
  predicted-correct categorizations and prove the comparison
  machinery works. They do not prove the new system is safe to take
  over decisions on merchants not yet seen. Re-running the script
  monthly (or whenever ≥5 new merchants flow through) keeps the
  baseline current; a clean exit 0 on a small corpus does not
  authorize cutover on its own.

### A.1 + A.2 decline-boundary policy
- **Spec shapes:** `docs/audit-confirmed-bugs.md`.
- **A.2** — fraud_score 65/70 threshold (decline cutoff).
- **A.1** — EOF gate (block parses that don't reach EOF).
- Both are decline-boundary policy calls. Shadow-first per the
  CLAUDE.md "decision-boundary changes" rule.
- Likely folded into the scoring-redesign Track A/B decisions; not
  shipped as standalone tweaks.

### NY broker-compensation disclosure guard
- **Spec / status:** the guard function `validate_broker_compensation_disclosure`
  at `src/aegis/compliance/broker_compensation.py` raises
  `NyBrokerCompensationDisclosureMissing` (cite: 23 NYCRR § 600.21(f))
  when a NY merchant is paired with a funder whose
  `aegis_compensation_disclosure_text` is empty.
- **Not wired into any production runtime path** (verified
  2026-06-05). A NY merchant can be matched to Shor (or any other
  funder with empty disclosure) without warning.
- **Decision needed:** wire as NY hard-fail in `match_funder`, OR
  delete the dormant module. Product question — AEGIS is now an
  internal pre-screening tool; funders own regulator-facing
  disclosures (per `.claude/rules/compliance.md` SCOPE NOTE).
- The new `/ui/funders/new` form already exposes
  `aegis_compensation_disclosure_text` as an editable field, so
  Shor's text can be pasted through the UI without any code change.

### Tampering rule shadow → live flip
- **Status:** shadow mode active. Audit-log action
  `tampering_would_decline` fires when the rule would have declined,
  but no actual decline is emitted.
- **Watch:** real fires in audit_log to see whether the rule's
  precision holds on live merchants (A&R KM and VU 7722 would have
  fired; review for false positives elsewhere).
- **Close the gap first:** persist `tampering_fires` at parse time
  (currently only fires through the score-time path; the persistence
  is the exactness gap).
- **Flip mechanism:** env var change, no code deploy.
- **Do NOT flip** until shadow audit rows reviewed against known
  good + known bad cases.

---

## Infra / systemic

Things that gate future work or limit how parallel we can be safely.

### Worktree isolation is broken
- **Observed:** 2026-06-05 — agents fell back to operating on main
  worktree repeatedly during the parallel build wave.
- **Why parallel was safe tonight:** files were strictly disjoint at
  the WRITE level (Track 1 wrote funder code + llm.py + funder
  templates; Track 2 wrote close client + close tests). Disjoint
  files were the actual protection, not the worktree.
- **Implication:** any future parallel batch that edits the SAME
  existing files (e.g. both touching `router.py` for unrelated UI
  work) is unsafe until isolation is fixed.
- **Fix:** investigate why worktree isolation fell back to main,
  ship a reproducible isolation mode. Gates future parallel-write
  work on shared files.

### `router.py` is monolithic
- **Current state:** ~2,800 lines, all `/ui/*` routes plus several
  `/api/*` ones, plus shared helpers.
- **Cost tonight:** parallel UI work (funder add-form + merchant
  add-form) was only file-disjoint because Agent B was reduced to
  a template-only change. If both had needed router edits, the
  parallel wave would have been a forced sequential.
- **Fix:** split per-domain into `src/aegis/web/routers/funders.py`,
  `merchants.py`, `close.py`, `dossier.py`, etc. Each importable as
  a FastAPI sub-router.
- **Status:** backlog; not blocking, but it would make parallel
  UI work routinely safe.

### Mapper test coverage shipped tonight — applies to future mappers too
- 117 structural-coverage tests landed for the
  storage / deals / funders / compliance mappers. Caught three real
  prod bugs (`_row_to_document` dropped chunk-B columns;
  `_row_to_deal` missing None-guard on state; `_doc_row_from_db`
  duplicated logic from `_row_to_document`).
- **Principle:** the in-memory backend is NOT a substitute for
  real-mapper coverage. Any new repository that ships a `_row_to_*`
  function must ship structural-coverage tests with it. See
  CLAUDE.md "external-integration test discipline" — same rule, same
  reason, same failure mode.

---

## Done + live tonight (for reference, not action)

So the next session knows what's in.

- **Funder UI**
  - `/ui/funders/new` manual create form (Wave 1A)
  - `/ui/funders/import` accepts multi-file + image uploads, merges
    extractions, ignores screenshot chrome (Wave 2 commits b85d81d
    → 023a6cb)
  - Funder extraction prompt tightened (split agent-clauses from
    merchant gating, canonical lowercase-hyphenated industries) —
    Shor accuracy DANGER bucket 4→1 PNG / 2→1 ISO, KEPT
- **Funder seed:** Shor Capital live in prod (`c5f05242…`)
- **Merchant UI:** `+ Add manually` button now reaches the existing
  `/ui/merchants/new` form (Wave 1B)
- **JSON parser bug fix:** `_first_json_object` uses
  `json.JSONDecoder().raw_decode` — affects every Bedrock JSON
  consumer (commit 06b771e)
- **Close client rewire:** `list_lead_attachments` walks note +
  email activity endpoints; `download_attachment` rewrites
  `app.close.com` → `api.close.com`. CloseAttachment.id synthesized
  from `sha256(url)[:16]` (commits 0067453 + 7184257). **Proven
  end-to-end on A&R KM's real Close lead** — pull (6 PDFs) → parse
  (5 docs reached terminal) → correctly gated.
- **`/ui/close-queue`** — pipeline state for every Close-sourced
  merchant; gated-rows-get-review-not-retry; STALE badge on stuck
  rows; gating-reason detail surfaces flag categories ("editor
  metadata + reconciliation drift" for A&R KM, "reconciliation
  drift + pattern signal" for VU). Commits 35a2fa7 + d2e49f1.
- **Spec persisted:** `docs/CLOSE_AUTOMATION_SPEC.md` (Option-A
  locked)
- **Tampering shadow mode:** building
  `bank_statement_tampering_confirmed` flag — fires audit row but
  no decline (commits 2d91a36 + 875d08a)
- **Dashboard redesign + fraud-signal legibility** — earlier waves
- **3 mapper bug fixes** (H2 / A.3 / H1 class) — caught by the new
  117-test mapper coverage suite

---

**Maintainer:** Filip.
**Update cadence:** edit at the close of any working session that
ships, parks, or surfaces new work. Keep the snapshot date current.
