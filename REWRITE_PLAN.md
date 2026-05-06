# AEGIS Python Rewrite Plan

Phased rewrite from TypeScript → Python. The TS repo (fk2204/aegis) stays
deployable until Phase 6 cutover. Every phase ends with a working artifact
the operator reviews before the next phase starts.

---

## Phase 0 — Project skeleton (1 day)

Goal: empty Python project that boots, has CI, has the right structure.

- [ ] `uv sync` installs all dependencies cleanly.
- [ ] Directory structure per CLAUDE.md exists.
- [ ] `Makefile` with `dev`, `test`, `typecheck`, `lint`, `format`, `worker`.
- [ ] `mypy --strict` config in `pyproject.toml`.
- [ ] Empty FastAPI app boots on `make dev` (port 5555).
- [ ] Health endpoint `GET /healthz` returns `{"ok": true}` and is the only
  route that does NOT require the bearer token.
- [ ] `src/aegis/config.py` loads env vars + enforces the data-residency
  boot guard.
- [ ] One trivial test passes via `make test`.

**Done when:** `make dev`, `make test`, `make typecheck`, `make lint` all
pass on a fresh checkout.

**STOP. Review with operator before Phase 1.**

---

## Phase 1 — Core models + math primitives (3 days)

Goal: data shapes and money math, no business logic.

- [ ] `src/aegis/money.py`: Decimal helpers (`as_money`, `to_cents`,
  `money_eq`, `safe_divide`). Includes the rule that `as_money(float)`
  raises (forces operator to convert at the boundary).
- [ ] `src/aegis/parser/models.py`: Pydantic models for `Transaction`,
  `ExtractedStatement`, `StatementSummary`, `ClassifiedTransaction`,
  `Aggregates`. Money fields use the `Money` annotated type. Every
  transaction has `source_page: int` and `source_line: int`.
- [ ] `src/aegis/scoring/models.py`: `ScoreInput`, `ScoreResult`,
  `FunderMatch`, `SubmissionPackage`.
- [ ] `src/aegis/compliance/apr.py`: APR calculator using
  `scipy.optimize.brentq` on the present-value-equals-zero equation.
  Cite CA 10 CCR 950 and 12 CFR § 1026 Appendix J in the docstring.
  Function: `calculate_apr(amount_financed, payments, disbursement_date)`.
- [ ] Property tests with `hypothesis` for APR: monotonic in factor,
  monotonic in term length.
- [ ] At least 5 known-answer test vectors for APR (computed by hand or
  cross-checked against Excel's RATE function).

**Done when:** APR passes property tests + 5 hand-computed vectors. All
models validate strictly.

**STOP. Review with operator before Phase 2.**

---

## Phase 2 — Parser pipeline (1 week, this is the biggest phase)

Goal: PDF in, validated + classified transaction list out, aggregates
computed in pure Python with full source attribution.

- [ ] `parser/metadata.py`: pikepdf-based metadata + EOF + xref + page-size.
  Fix the personal-author regex bug (don't flag "Bank Of America" as a
  personal name).
- [ ] `parser/prompts.py`: TWO prompts.
  - `EXTRACTION_PROMPT`: asks Claude for the raw transaction list with
    page/line numbers, plus the printed statement summary. NO aggregates,
    NO classification.
  - `CLASSIFICATION_PROMPT`: takes a list of transactions and returns a
    list of {transaction_id, category, confidence}.
- [ ] `parser/extract.py`: pass 1 — Bedrock call, JSON parse, Pydantic
  validation. Returns `ExtractedStatement` (raw transactions + summary).
- [ ] `parser/validate.py`: deterministic gate.
  - Daily reconciliation: for each day with transactions, verify the
    running balance ties out within $1.
  - Period reconciliation: beginning + sum(deposits) − sum(withdrawals)
    = ending within $1.
  - Listed-vs-summary tie-out: sum of extracted matches printed totals.
  - Statement period 14–50 days.
  - Source attribution: every transaction has source_page and source_line.
  Returns `ValidationResult(passed, failures, warnings)`. ANY failure
  marks the doc `manual_review`. No retry.
- [ ] `parser/classify.py`: pass 2 — Bedrock call, batched (50
  transactions per call to stay within token limits). Returns a
  classified transaction list. Per-row confidence stored.
- [ ] `parser/patterns.py`: 19 detectors from the TS version, ported.
  Operate on classified transactions. Use numpy for CV/variance. Fix
  the false-positive surfaces flagged in the TS review (generic-word
  MCA detection, weekend deposits for cash-heavy retailers).
- [ ] `parser/aggregate.py`: deterministic aggregation.
  - `avg_daily_balance` from the running balance series.
  - `true_revenue` = sum of `deposit` and `ach_credit` rows minus
    `transfer` and `chargeback` rows.
  - `num_nsf` from rows classified `nsf_fee`.
  - `days_negative` from running balance.
  - `mca_daily_total` from rows classified `mca_debit`.
  - Every aggregate returns a tuple of (value, source_transaction_ids).
- [ ] `parser/pipeline.py`: orchestrator. Same fraud-score weighting
  (35/40/25) and compound escalation as TS. ONE threshold constant
  imported from config (fix the three-different-thresholds bug).
- [ ] Migration to add the `transactions` table:
  ```sql
  CREATE TABLE transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id),
    merchant_id UUID REFERENCES merchants(id),
    posted_date DATE NOT NULL,
    description TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL,
    running_balance NUMERIC(14,2),
    source_page INT NOT NULL,
    source_line INT NOT NULL,
    category TEXT,
    classification_confidence INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
  );
  CREATE INDEX idx_transactions_document ON transactions(document_id);
  CREATE INDEX idx_transactions_merchant ON transactions(merchant_id);
  CREATE INDEX idx_transactions_category ON transactions(merchant_id, category);
  ```
- [ ] Migration to add `_source_ids: UUID[]` arrays to every aggregate
  field on the `analyses` table.
- [ ] Port the 117 existing test cases from the TS repo to pytest. Use
  the synthetic PDFs in `test-data/`.
- [ ] Parity test: same PDF through TS and Python systems; new system's
  aggregates match TS within tolerance, except for documented fixes.

**Done when:** All ported tests pass. Source attribution verified end to
end (a parsed PDF produces transactions with non-null source_page/line,
and aggregates have non-empty source_ids arrays).

**STOP. Review with operator before Phase 3. This is the critical
review point — confirm the architecture is what you wanted.**

---

## Phase 3 — Scoring + matching (3 days)

- [ ] `scoring/score.py`: hard declines + soft scoring. Same rules as
  TS. Fix `estimated_payback_days` to use `total_repayment / daily_payment`,
  not principal. Use Decimal throughout.
- [ ] `scoring/build_score_input.py`: Supabase queries, 90-day staleness
  check. Read `statement_days` from a real column (add migration).
- [ ] `scoring/match_funders.py`: add soft-concern for missing credit
  score / TIB instead of silent pass.
- [ ] `scoring/submission_package.py`: REWRITE the term/payback math. The
  TS version's email body has numbers that don't reconcile.

**Done when:** Scoring tests pass + new tests for fixed bugs.

**STOP. Review with operator before Phase 4.**

---

## Phase 4 — Compliance (1 week — this is the slow phase)

Goal: state regulation table that is correct, with state-prescribed
disclosure templates.

- [ ] **Statute audit.** For each of CA, IL, NC, NY, NJ, VA, CT, UT, FL,
  GA, KS, MD, TX, MO: operator provides the actual bill text or a link
  to the statute. Claude Code reads it and fills in `compliance/states.py`
  with verified values + citation comments. Do NOT fill in values from
  prior knowledge — every constant gets a cited source.
  - Specifically verify (these were wrong in the TS version): Kansas SB 345
    (enacted), Georgia SB 90 (enacted), Missouri SB 1359 (enacted),
    Maryland's actual current bill (not "HB 1007"), Virginia CoJ
    prohibition.
- [ ] **State-prescribed disclosure templates.** For each state with
  `disclosure_required=true`, find the regulator's prescribed form
  (CA DFPI, NY DFS, VA SCC, CT DOB, FL OFR, etc.). Operator provides
  the template URL or PDF; Claude Code renders Jinja templates that
  match exactly.
- [ ] `compliance/disclosure.py`: state-router. If no template exists
  for a state with `disclosure_required=true`, raise
  `DisclosureTemplateMissing`. Never fall back to generic.
- [ ] Snapshot test per state: render disclosure with fixed inputs,
  snapshot the HTML.
- [ ] HTML escaping via Jinja autoescape. Never string-format user
  input into HTML.

**Done when:** Every state in `states.py` has a citation. Every state
with active disclosure obligations has a regulator-prescribed template.
Snapshot tests pass.

**STOP. Operator MUST review every state's template against the
regulator's prescribed form before approving Phase 5. This is where
non-compliance becomes regulatory exposure.**

---

## Phase 5 — API + Zoho + dashboard (1 week)

- [ ] `api/auth.py`: bearer token, constant-time compare.
- [ ] `api/routes/upload.py`: rate limit, 25MB size limit, discard
  filename, write as `uuid4().hex + ".pdf"`. Cleanup in `finally`.
- [ ] `api/routes/transactions.py` (NEW): per-merchant transaction
  listing for audit drill-down. Filter by category, date range, page.
- [ ] Other API routes: ports of merchants, deals, funders, disclosures.
- [ ] `zoho/client.py`: OAuth refresh + retries with `tenacity`.
- [ ] `zoho/sync.py`: outbound + inbound. UNIQUE constraint on
  `merchants.zoho_deal_id`. Use `INSERT ... ON CONFLICT DO NOTHING`
  for idempotency.
- [ ] `api/routes/webhooks_zoho.py`: HMAC + timestamp freshness check
  (reject sigs > 5 min old).
- [ ] `arq` worker config (replaces in-memory queue).
- [ ] `web/`: minimal HTMX dashboard. The merchant-detail page MUST
  support drill-down: clicking an aggregate (e.g. "Total Deposits:
  $47,300") shows the contributing transactions with page/line refs.

**Done when:** Upload → parse (two-pass) → score → disclosure → Zoho
sync works end to end. Dashboard usable. Drill-down works.

**STOP. Review with operator before Phase 6.**

---

## Phase 6 — Parity validation + cutover (3 days)

- [ ] Run a corpus of 30+ statements through both old and new systems.
  Diff outputs.
- [ ] Investigate every diff. Python should be correct (or known-better).
- [ ] Deploy new system to Hetzner. Old system stays on a different port
  for one week as fallback.
- [ ] After one week: archive old repo, remove fallback.

**Done when:** New system is live, old system archived.

---

## What I'm NOT doing

- Adding features. Behavior parity (with bug fixes) only.
- Optimizing for scale. Get correctness first.
- Fancy dashboard. HTMX is enough.
- Replacing Supabase, Zoho, Hetzner, Cloudflare.

---

## Estimated total

3-5 weeks of Claude Code sessions, depending on Phase 4 statute-audit pace.
