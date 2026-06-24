# AEGIS — Remaining work

**Snapshot taken:** 2026-06-24 — overnight update. **Step 2 scoring
cutover CLOSED** 2026-06-23 (commits c5c11fc + a80a388): track_abc
now drives live decline decisions, legacy fraud_score informational at
the scorer layer (parser-layer gate remains, separate retirement). H2
+ H4 + H5 + H7 + H8 + H9 hygiene findings all CLOSED in the same
session. Punch-list now centered on the new 2026-06-23 audit findings
(operational + code/docs hygiene + cron wiring) — see section at the
bottom. Earlier carryover: F3-F11 (Track A audit doc) still open.
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

### Step 2 of 3-track redesign — `fraud_score` retirement + A+B+C live cutover ✅ CLOSED 2026-06-23 (`c5c11fc` docs / `a80a388` chmod + reset / track_a EXIT 0)
- **Status:** **FLIPPED.** `AEGIS_SCORING_ENGINE=track_abc` set in
  `/etc/aegis/aegis.env` on the prod box 2026-06-23. Track A
  integrity verdict + Track B band drive live decline decisions;
  legacy `fraud_score` is informational only at the scorer layer
  (parser-layer `fraud_score >= 65` gate remains in
  `src/aegis/parser/pipeline.py` — known, documented, separate
  retirement).
- **Validation that authorised the flip:**
  `scripts/track_a_historical_lookback.py` ran clean on the prod
  corpus 2026-06-23 — EXIT 0, 42 legacy-declined docs scanned, 0
  misses. All 42 correctly identified as decline under Track A (mix
  of `clean` + `strong_metadata` branches). Authorisation log entry
  in `docs/STEP_2_CUTOVER_REVIEW.md`.
- **Pre-cutover protective work** (same session): 5 new
  `bank_layouts` hints (`584344e`); `--reparse-sealed-manual-review`
  flag (`11fe64d`) with chmod-0644 + dead-code-reset (`a80a388`);
  root-cause tempfile leak fix in workers.py via
  `keep_local_plaintext` flag (`22d3d1e`).
- **Below is the original gating block, preserved for historical
  reference.** All four conditions met by the lookback run + the
  authorisation log entry.
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
- **Review script:** ✅ `scripts/tampering_shadow_review.py`
  (2026-06-12). Read-only diagnostic: pulls every fire row from
  `audit_log` (both shadow + live actions so it keeps working
  post-flip), groups by branch / contributing-failure / mode, writes
  CSV to stdout + summary to stderr. Exit 3 if any rows present
  (operator review queue non-empty). Run on the box with
  `/etc/aegis/aegis.env` sourced. 28 unit tests against a fake
  Supabase client + defensive-parsing fixtures cover every shape the
  prod write path emits.
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

## 2026-06-23 audit findings

Three parallel read-only agents covered docs/code drift, test/CI
hygiene, and live prod state after the Step 2 scoring engine cutover.
Closure status per the post-cutover-session work (`22d3d1e` and the
companion items 3–6).

- 🔴 **Tempfile leak in `--reparse-sealed-manual-review`** ✅ CLOSED
  2026-06-24 (`22d3d1e`) — root-cause fix in `workers.py` via new
  `keep_local_plaintext: bool = True` parameter on `parse_document` →
  `_run_processor_branch` → `_try_pdf_store_step`. When False AND the
  pdf_store seal fails, `_safe_unlink` runs before returning False.
  Default True preserves every existing call site byte-identically;
  reparse path passes False since the encrypted copy already exists
  in pdf_store. 100ms `asyncio.sleep` paces the per-doc enqueues to
  prevent the burst that caused 16 `pdf_store.storage_upload_failed`
  events on 2026-06-23. Same commit removes the now-dead
  `_PERMISSION_BUG_DOC_IDS` constant + `--reset-parse-status` flag.
- 🟡 **Duplicate `AEGIS_SCORING_ENGINE`** in `/etc/aegis/aegis.env`
  ✅ CLOSED 2026-06-24 — `awk '!seen[$0]++'` dedupe on the box
  (no commit; prod ops only). Single occurrence at line 29 confirmed.
- 🟡 **Unexplained 02:11 UTC restart** ✅ CLOSED 2026-06-24 — `last`
  + `journalctl` traced to GH run `28070383602` (deploy workflow on
  `c5c11fc`, started 02:11:31, sudo systemctl restart at 02:11:44,
  both units back at 02:11:47). Routine CI auto-deploy that the
  audit had missed connecting to a long queue gap. Not a real
  anomaly.
- 🟡 **`pdf_store.storage_upload_failed` x16 during reparse burst**
  ✅ CLOSED 2026-06-24 (`22d3d1e`) — root-cause was the un-paced
  26-job burst; the 100ms pacing in the same commit prevents
  recurrence. Worker-side handler was already graceful (audit row +
  preserve plaintext) — no half-state rows ever existed.
- 🟡 **Tempfile sweep on the box** ✅ CLOSED 2026-06-24 — manual
  `rm /var/lib/aegis/uploads/recover_legacy_docs_script-reparse-*.pdf`
  on the box swept the 26 leaked files from the 2026-06-23 reparse
  run. ~12 MB reclaimed. Going forward the worker-side fix
  (`22d3d1e`) prevents recurrence.
- 🟡 **Zero test coverage on `--bump-parse-count` /
  `--reparse-sealed-manual-review`** — addressed by item 4 of this
  session (`tests/scripts/test_seed_bank_hints.py` +
  `tests/scripts/test_recover_legacy_docs.py`).
- 🟡 **Track A lookback not wired as recurring cron** — addressed by
  item 5 of this session (weekly Mon 06:00 UTC arq cron).
- 🟡 **New operator flags not in RUNBOOK** — addressed by item 6 of
  this session (`deploy/RUNBOOK.md` or equivalent canonical surface).

---

## 2026-06-12 audit findings — operator triage queue

Two read-only audits dispatched and landed alongside the tampering
review script. Findings here are NOT acted on; they're queued for the
next decision-boundary-touching session because they need operator
judgment (severity calls, hook-config flips, scope decisions).

### `docs/track_a_audit_2026-06-12.md` — Track A integrity verdict
- **F1a HARD ✅ CLOSED 2026-06-12 (`67797aa`)** — `short_editor`
  truncated to 60 chars in `framing.py` (57 + `...` ellipsis) so the
  drift_plus_editor rationale stays under the `max_length=320` Pydantic
  cap with worst-case editor signatures. Preserves the leading vendor
  name (the underwriting-actionable part). Test
  `test_long_editor_string_truncates_to_keep_rationale_under_max_length`
  in `tests/scoring_v2/track_a/test_integrity_verdict.py`.
- **F1b HARD ✅ CLOSED 2026-06-12 (`eb6bf7a`)** — catch-all in
  `compute_score_deal_track_inputs` split: `ValidationError` now logs
  CRITICAL with full exc repr (code bug, page someone); generic
  `Exception` still WARNING (data oddity). Log-prefix preserved so
  existing structured-log monitors continue to match. Tests in
  `tests/scoring_v2/test_score_deal_track_inputs.py`.
- **F2 HARD ✅ CLOSED 2026-06-12 (`c3beb25`)** — strong-metadata branch
  1 in `compute.py:93-101` now mirrors branch 2's evidence-build loop:
  every `drift_failure` surfaces as its own `EvidenceItem` alongside
  the metadata rows when drift IS present. Underwriter sees the full
  corroboration on the dossier, not just the metadata score. Test
  `test_strong_metadata_with_drift_surfaces_both` in
  `tests/scoring_v2/track_a/test_integrity_verdict.py`.
- **F3-F11** — 6 WORTH + 3 INFO findings: boundary-test gaps,
  drift-only mirror in `all_flags`, lookback-script flag-prefix
  coupling, schema decline-field guard for `UnifiedTracksView`,
  others. See doc for full punch-list. **Still open — next punch-list
  after the forensic integrity layer ships.**

### Forensic integrity layer 🟡 IN-PROGRESS 2026-06-24
Three deterministic detectors plumbed in over four commits:

- **F-forensic-1 ✅ Commit `3fb6692` (local)** — `aegis.parser.forensic.font_consistency`
  row-level font/size mismatch detector via pymupdf. Modal-family +
  uniform-size guard so heading-vs-body legitimate variation doesn't
  fire; flags when transaction spans use a non-modal family, or differ
  in size by >1pt, or >20% of non-tx spans differ. Wired into
  `analyze_metadata` (+15 to metadata_score) and surfaces as
  `[META] font_inconsistency_detected: ...`.
- **F-forensic-2 ✅ Commit `df0946e` (local)** — `aegis.parser.forensic.creator_fingerprint`
  per-bank PDF /Creator mismatch via `KNOWN_CREATOR_PATTERNS` registry
  (BoA / Chase / TD / Third Coast — all Adobe-family). Fires when the
  /Creator names an editing tool (PDFlib, iText, Sejda, Foxit, etc.)
  AND the parsed bank is registered AND the detected tool doesn't
  match the bank's known-good profile. Wired into `run_pipeline`
  POST-extraction (needs `bank_name`) at the same call site as the
  validation gate; +20 to `metadata.fraud_score` propagates into
  `_decide` because the check now runs BEFORE `_fraud_score`. Flag
  form `[META] creator_mismatch_detected: detected=...; editing_tool=...; expected_one_of=[...]`.
  **OPERATOR-ACTION**: verify the registry against real-merchant
  statements — first few entries are placeholder Adobe-family; needs
  ground-truth confirmation before it gates anything.
- **F-forensic-3 ✅ Commit `1b61bd7` (local)** — `aegis.parser.forensic.text_overlay`
  Y-range overlap detector across multi-stream pages via pikepdf
  content-stream parsing. The paste-over fraud signature: original
  bank-issued stream + second attack stream rendering replacement
  text at the same Y coordinates. Wired into `analyze_metadata`
  (+25 to metadata_score — strongest of the three because it's direct
  evidence of content-stream manipulation). Flag form
  `[META] text_overlay_detected: page(s) <list>; streams=<n>`.
- **F-forensic-4 ✅ (this commit, local)** — Track A wire-through:
  `extract_forensic_signals` helper in `scoring_v2/track_a/signals.py`
  surfaces all three forensic flags as their own `EvidenceItem` rows
  on EVERY branch of `compute_integrity_verdict` (including `clean`,
  so the underwriter sees a standalone forensic finding for awareness
  even when no branch fired). De-dupes when the same flag is mirrored
  into both `metadata_flags` and `validation_failures` via
  `_collect_flags`. Plain-English rationale per signal +
  `EvidenceItem.detail` 240-char truncation guard (mirrors F1a). Tests
  in `tests/scoring_v2/track_a/test_integrity_verdict.py` (7 new).

**VyStar Credit Union — creator registry pending.** Two statements
seen, both `manual_review` status — insufficient proceed-corroborated
baseline to whitelist any creator string. Operator action: when the
next VyStar statement parses to `proceed`, check its /Creator field
and add it to `KNOWN_CREATOR_PATTERNS` if it matches a known-good bank
export tool.

**Shipping plan**: batch push of all four commits as one unit, hold
for operator approval. After ship: F3-F11 (Track A audit findings) is
the next punch-list, then operator's queued items — offer sizing wired
into funder matching, image-only banks auto-routed to vision mode.

### `docs/repo_hygiene_2026-06-12.md` — Repo / tooling hygiene
- **H1 act-now ✅ CLOSED 2026-06-12 (`bcf209a`)** — `make install-hooks`
  no longer sets `core.hooksPath=.githooks`. The compliance-review
  check now lives as a third local hook inside `.pre-commit-config.yaml`
  (entry shells to `.githooks/pre-commit` so standalone diagnosis still
  works) — single pre-commit framework wires ruff + ruff-format + mypy
  + compliance-review under one entry point. Clones on the old path
  are migrated automatically (unset on next `make install-hooks`).
  Verified by direct hook test: reject without annotation, pass with
  `not-applicable` / `approved by <name>`.
- **H2 worth-fixing ✅ CLOSED 2026-06-23 (`eba8a2f`)** — moved brand
  assets (`logo-preview.html` + 6 SVGs) to `docs/brand/`; moved
  `_audit_seed.py` + `_audit_seed_funders.py` to `scripts/audit/`;
  added `design/` to `.gitignore` with comment ("Local design
  artifacts — not for repo"). Scripts each carry op-principle-#4
  warnings in their docstrings.
- **H3 worth-fixing ✅ CLOSED 2026-06-12 (`dcc65c1`)** — Four 2026-06-10
  ops gotchas (sudo NOPASSWD literal form, systemctl-status token leak,
  install-script token grep, read-rules-first) landed as a new
  "Box-side operations gotchas" section in `.claude/rules/deploy.md`,
  plus the funder seeding sub-rule extension to operating-principles
  Rule 4. CLAUDE.md footer refreshed.
- **H6 ✅ ACTED ON 2026-06-12** — added `*.bak`, `*.swp`, `*~`,
  `.aider*` to `.gitignore`. The trivial one — other gitignore
  coverage is already strong on credentials/tokens.
- **H4 CLAUDE.md modules table ✅ CLOSED 2026-06-23 (`8785325`,
  refreshed in `c5c11fc`)** — `business_intel/` + `web_presence/`
  added to the first-class-modules table; `scoring/` + `scoring_v2/`
  refreshed to reflect track_abc as the live engine post-cutover.
- **H5 `fraud_score` framing ✅ CLOSED 2026-06-23 (`8785325`,
  refreshed in `c5c11fc`)** — historical context quote block + the
  modules table rows updated. CLAUDE.md no longer claims legacy
  drives production; clarifies parser-layer gate is a separate
  retirement item.
- **H6 ✅ ACTED ON 2026-06-12** — added `*.bak`, `*.swp`, `*~`,
  `.aider*` to `.gitignore` (kept here for completeness).
- **H7 deploy doc `uv sync --locked` step ✅ CLOSED 2026-06-23
  (`8785325`)** — the on-box `uv sync --locked` step (added to
  `.github/workflows/deploy.yml` on 2026-06-16 to fix the
  ProtectSystem=strict outage) is now documented in the CLAUDE.md
  deploy paragraph.
- **H8 stale test skips ✅ CLOSED 2026-06-23 (`1f6a928`)** — deleted
  `tests/test_quarterly_disclosure_render.py` (dead Phase 3
  placeholder, real coverage in `tests/compliance/test_disclosure_tier1_context.py`);
  deleted the conditional refer-mapping skip in
  `tests/test_score_decision_snapshot.py`. Also fixed `pytest_*.log`
  in `.gitignore` (`aa2d174`).
- **H9 Decimal→float cast comments ✅ CLOSED 2026-06-23 (`2bfadff`)** —
  `src/aegis/scoring/score.py:1013` and `src/aegis/compliance/apr.py:113-122`
  now carry justification comments explaining why the float cast is
  necessary (scipy/statistics math + Decimal's no-fractional-exponent
  limitation; round-trip back to Decimal at storage).
- **H10** — informational / docs-archival residual from the
  2026-06-12 hygiene doc; not actioned this session.

### Tampering shadow review script ✅ SHIPPED 2026-06-12
`scripts/tampering_shadow_review.py` — see the gating-conditions
update under "Tampering rule shadow → live flip" above.

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
