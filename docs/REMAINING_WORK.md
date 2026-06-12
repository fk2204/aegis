# AEGIS — Remaining work

**Snapshot taken:** 2026-06-11 (post-session closure pass: 2026-06-10
session work + the "Add funder via Claude Code" wrapper + funder
upsert audit emit + worktree-isolation diagnostic + router.py-split
already-done verification. Nine items moved from open to ✅ CLOSED
with resolving commits inline. See the "2026-06-10 closure log" at
the head of each section for what shipped).
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

### "Add funder via Claude Code" ✅ CLOSED 2026-06-11
Shipped as `scripts/add_funder.py`. Two subcommands:

```
python scripts/add_funder.py extract <file>... [--output PATH]
python scripts/add_funder.py save --from PATH [--dry-run]
```

`extract` reads PDF/PNG bytes, routes through the existing
`extract_funder_guidelines` / `extract_funder_guidelines_from_image` /
`merge_extractions` engine, prints the merged `FunderGuidelineExtraction`
as JSON to stdout (or `--output`) plus a human-readable summary with
low-confidence fields (<60) to stderr. `save` re-reads the (possibly
edited) preview and calls `FunderRepository.upsert`. Two-phase by
design so Claude Code can show fields in chat between extract and
save — CLAUDE.md "extraction assists, never replaces judgment" is
enforced by the workflow, not the CLI. `--dry-run` validates round-trip
without writing.

Pure-function core + DI-injected IO (LLM client, repo, file readers)
so the 26-test suite at `tests/scripts/test_add_funder.py` runs
against `InMemoryFunderRepository` + a stub LLM. No new write path,
no new prompt.

Audit emit added in the follow-up commit (2026-06-11): the wrapper's
`save` mode records a `funder.imported` row with `actor="claude_code"`
after a successful upsert. Mirror change shipped to
`/ui/funders/import/save` (`funder.imported`) and `/ui/funders/new`
(`funder.created`) routes so all three call sites are now compliant
with the CLAUDE.md "audit log written for every state change" rule.
The audit lives at the CALL SITE, not in the repo (matches the
established `funder.reextracted` / `funder.operator_notes_updated`
pattern at `src/aegis/web/routers/funders.py`).

### Funder-extraction prompt — third rule ✅ CLOSED 2026-06-10 (`b07bfe1`)
Rule 11 added as syntactic subject-test on top of Rule 9's semantic
enumeration: inspect the grammatical subject of each clause before
deciding the bucket. If subject is the agent / ISO / broker /
referring party / "you", the obligation is agent-side regardless of
topic — routes to `notes_residual`. Reinforces default-to-residual
posture. 95 existing funder tests still pass; prompt-only change.

Historical context preserved: Shor's ISO retest still leaked CA/VA
agent-compliance clauses into `conditional_requirements` at confidence
72 after the first prompt-tighten, motivating Rule 11.

### `/uploads/from-close` route — xfail debt ✅ CLOSED 2026-06-10 (`201bb2d`)
Route now calls `list_lead_attachments(body.close_lead_id)` before
`download_attachment(body.attachment_id)` inside the existing try
block. Cache-miss after a successful list call mapped to 404
(semantically the attachment doesn't belong to this lead). All 5
previously-xfailed tests in `tests/api/test_uploads_from_close.py`
removed from xfail and passing. Mock transport refactored to
dispatch by URL path.

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
- **Diagnostics in place:**
  - `scripts/shadow_comparison_a_b_c_vs_fraud_score.py` (commit `973d7fd`).
    Read-only sweep — every merchant in the corpus, LIVE decision vs
    new A/B/C, categorised per disagreement bucket. Re-run periodically;
    treat exit code 3 (any `old-caught-something-new-misses` row) as a STOP.
  - `scripts/track_a_historical_lookback.py` (commit `618bdea`, Wave 4.1).
    Walks every document whose `fraud_score >= HARD_DECLINE_THRESHOLD`
    and runs `compute_integrity_verdict` on persisted Track A signals.
    Exit code 3 = at least one miss row (REGRESSION — operator triage
    required). Read-only; box-run, not laptop-run.
  - `docs/STEP_2_CUTOVER_REVIEW.md` (commit `2fc11ff`, Wave 4.3). The
    operator's triage workspace — three honest disagreement categories
    (`genuine-regression`, `detector-gap`, `corpus-shape-artifact`),
    row template, authorisation log. Gates the env-var flip on zero
    un-reviewed open rows.
- **Cutover gating conditions — ALL required, in order:**
  1. **Corpus growth.** The 2026-06-06 baseline is N=1 for Track B/C
     comparison (only VU had classified transactions; A&R KM had docs
     but the `manual_review` path persisted no analyses). A cutover
     decision on N=1 is not a population claim, it is a case
     observation. Need significantly more deals through both systems
     before "no regressions" means anything.
  2. **Track A historical lookback.** ✅ Script shipped 2026-06-10.
     Awaits operator-run against prod corpus + miss-row triage via
     `STEP_2_CUTOVER_REVIEW.md`.
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

### A.1 + A.2 decline-boundary policy ✅ CLOSED 2026-06-10
- **A.2** — `4c1c743` aliased `FRAUD_SCORE_HARD_DECLINE` to
  `HARD_DECLINE_THRESHOLD = 65`; regression test pins equality at the
  boundary. `fcecdb2` refreshed 6 stale "≥ 70" references in
  config / env-var docs / flag glossary / scorer docstring.
- **A.1** — box-side env var `AEGIS_EOF_THRESHOLD=2` set in
  `/etc/aegis/aegis.env` on 2026-06-10; aligns scorer with the
  pipeline's R4.6 narrative (EOFs=2 is normal for legit online-banking
  exports, not a hard decline).
- Both items documented in `docs/audit-confirmed-bugs.md` header as
  ✅ CLOSED with their resolving commits.

### NY broker-compensation disclosure guard ✅ CLOSED 2026-06-10 (`6f595a4`)
`match_funder` now calls `validate_broker_compensation_disclosure`
next to the existing `excluded_states` check. NY merchant paired with
a funder whose `aegis_compensation_disclosure_text` is empty surfaces
as a hard-fail on the match grid with reason
`broker_compensation_text_missing`. Catches the base
`BrokerCompensationDisclosureMissing` so future states added to
`_STATE_RULES` auto-apply without further edits. Three regression
tests (NY-empty / NY-present / CA-empty pass-through) in
`tests/funders/test_match_regression.py`. 456 funder + compliance
tests still green.

### Tampering rule shadow → live flip
- **Status:** shadow mode active. Audit-log action
  `tampering_would_decline` fires when the rule would have declined,
  but no actual decline is emitted.
- **Watch:** real fires in audit_log to see whether the rule's
  precision holds on live merchants (A&R KM and VU 7722 would have
  fired; review for false positives elsewhere).
- ✅ **Parse-time persistence gap CLOSED 2026-06-10 (`14c9671`):**
  `documents.all_flags` now carries the tampering verdict from
  parse time —
  `[SHADOW] bank_statement_tampering_confirmed:<branch>` while in
  shadow, `[META] bank_statement_tampering_confirmed:<branch>` after
  the flip. Visible on the dossier; close-queue ignores SHADOW prefix
  by design. Not a decision-boundary change — the score-time decline
  gate still reads `deal.tampering_confirmed`.
- **Flip mechanism:** env var change, no code deploy.
- **Do NOT flip** until shadow audit rows reviewed against known
  good + known bad cases.

---

## Infra / systemic

Things that gate future work or limit how parallel we can be safely.

### Worktree isolation is broken — documented + workaround in use
- **Observed:** 2026-06-05 and 2026-06-10 — agents launched with
  `isolation: "worktree"` modify the parent repo's working tree
  instead of a side worktree on Windows.
- **Diagnostic doc:** ✅ `docs/worktree_isolation_diagnostic.md`
  (2026-06-11) — concise repro + workaround + signals to watch for
  when the platform fix lands.
- **Workaround in use:** trust file-disjointness, not the flag. The
  orchestrator scopes each parallel agent to a strictly disjoint set
  of paths at the WRITE level; shared-file edits (REMAINING_WORK.md,
  CORPUS_FINDINGS.md, etc.) happen in the parent agent after sub-
  agents return. Proven on 2026-06-11 by the audit-emit + worktree-
  doc parallel fan-out.
- **Status:** open as a Claude Code platform bug. AEGIS-side
  mitigation in place; revisit when the platform docs/release notes
  confirm the flag is honored on Windows.

### `router.py` is monolithic ✅ CLOSED 2026-06-11 (verified post-split)
Tracker entry was stale. `src/aegis/web/router.py` is now 131 lines —
a composition layer over per-domain sub-routers under
`src/aegis/web/routers/`:

```
admin.py             close_queue.py       compliance.py
dashboard.py         disclosure_events.py documents.py
funders.py           intake.py            merchants.py
portfolio.py         renewals.py          triage.py
upload.py
```

13 domains extracted. The big-monolith concern motivating this entry
no longer applies — parallel UI work that touches disjoint domains is
already routinely safe. If a NEW domain emerges that warrants its own
file (e.g. a future `submissions.py` per Phase 7C plans), extract it
at that point; nothing more to do as standing infra work.

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

## 2026-06-10 closure log

What shipped in this session (15 commits, all on origin/main through
`20f60ec`). Closures referenced above; this is the consolidated index:

- **A.2 + audit closure** — `4c1c743` aligned `FRAUD_SCORE_HARD_DECLINE`
  → 65; `fcecdb2` refreshed 6 stale "≥ 70" references; `1633d4a`
  marked A.1/A.2/A.3 ✅ CLOSED in `docs/audit-confirmed-bugs.md`.
- **A.1** — box-side `AEGIS_EOF_THRESHOLD=2` set in `/etc/aegis/aegis.env`.
- **Funder-extraction prompt Rule 11** — `b07bfe1` syntactic
  agent-duty subject-test ON TOP of Rule 9's semantic enumeration.
- **NY broker-comp guard wired** — `6f595a4` in `match_funder` (catches
  the base class, future states auto-apply).
- **`/uploads/from-close` URL-cache prime** — `201bb2d`; 5 xfails removed.
- **Tampering parse-time persistence** — `14c9671`
  `[SHADOW]/[META] bank_statement_tampering_confirmed:<branch>` on
  `documents.all_flags`.
- **Track A historical lookback script** — `618bdea` Wave 4.1, with
  injected DI Protocol for test isolation, 14 tests green.
- **Cutover-review workspace + industry normaliser** — `2fc11ff`
  Wave 4.3 (`docs/STEP_2_CUTOVER_REVIEW.md`) + 1.4
  (`scripts/normalise_funder_industries.py` in dry-run-default mode).
- **Manual back-fill cleanup** — `6682c30` (3 scripts removed),
  preceded by `60ce74d` operator-directed acceptance-set trim.
- **Close-queue stale-row tests** — `5f937d9` pinned 6h-pull / 1h-parse
  predicates (12 cases).
- **Security allow-list** — `93c04c2` github.com added under
  documentation-references (admin health page repo URL).
- **CF_API_TOKEN convention** — `f5ff460` added, then `20f60ec` reverted
  per operator direction (no CF-side automation planned; canonical
  health monitoring is the push-based `aegis-heartbeat-*` timers).

---

## Done + live previously (for reference, not action)

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
