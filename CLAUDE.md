# CLAUDE.md — AEGIS Python Rewrite Project Agreement

**Project-specific working agreement for Claude Code sessions.**

---

## Mission

AEGIS is an MCA (Merchant Cash Advance) underwriting brain for Commera Capital,
being rewritten in Python for mathematical accuracy, auditability, and
regulator-defensibility.

- **What it does:** Parse bank statements (4-layer fraud detection) → score
  deals → generate state-compliant disclosures → sync with Zoho CRM
- **Scale:** Solo operator, ~100 deals/month, internal-only
- **Why rewrite:** TS version had simple-interest APR (wrong for CA/NY), float
  money math, hand-rolled numerics, single-pass extraction with no audit
  trail to source rows, and stale state regulation data.

**Current phase:** Phase 7A complete (dashboard UI live). Phase 7B ships funder
matching + per-funder submission package (CSV ZIP forwarded to funders) + live
Today-page KPIs sourced from Supabase + audit_log. Phase 7C (durable
submissions table, post-funded servicing) still pending. See `REWRITE_PLAN.md`
for the full phase tracker.

---

## Architecture (read this carefully — different from the TS version)

### Two-pass parser
The parser does NOT ask Claude for aggregates. Aggregates are derived from
transaction rows in pure Python. The flow is:

1. **Metadata layer** — pikepdf inspects PDF tampering signals.
2. **Extraction (Claude pass 1)** — Claude returns the raw transaction list
   (every row, with date, description, amount, page number, line number,
   running balance if present) AND the printed statement summary (beginning
   balance, ending balance, deposit total as printed, withdrawal total as
   printed, statement period). NO classification yet, NO derived aggregates.
3. **Validation (deterministic)** — Python code verifies:
   - Sum of extracted deposits matches printed deposit total within $1
   - Sum of extracted withdrawals matches printed withdrawal total within $1
   - Beginning + sum(deposits) − sum(withdrawals) = ending, within $1
   - Daily running balance reconciles for every day with transactions
   - Number of extracted transactions matches printed count if available
   If ANY check fails, parse_status = "manual_review". No retry, no second AI
   chance. The validation gate is the firewall against AI hallucination.
4. **Classification (Claude pass 2)** — Each transaction in the validated list
   is classified into one of: `deposit`, `payroll`, `ach_credit`,
   `mca_debit`, `nsf_fee`, `wire_in`, `wire_out`, `transfer`, `fee`,
   `chargeback`, `refund`, `other`. Misclassifying one row is recoverable;
   misclassifying many shows up as low confidence and triggers review.
5. **Patterns (deterministic)** — MCA stacking, kiting, preloan spike, wash
   pairs, paydown detection. Pure Python over the classified rows.
6. **Aggregation (deterministic)** — Computes avg_daily_balance, true_revenue
   (deposits net of transfers and chargebacks), num_nsf, days_negative,
   debt_to_revenue, etc. Every aggregate stores the list of transaction_ids
   that produced it.

### Audit trail
Every aggregate field in the final report MUST be traceable to specific
source transactions. The `analyses` table stores not just `total_deposits`
but also `total_deposits_source_ids: list[uuid]` referencing the
`transactions` table, where each transaction has `source_page` and
`source_line` from the original PDF. When a funder asks "where did this
come from?" the answer is "page 7 lines 14, 22, 31, ..." — clickable.

### Storage
- `documents` — one row per PDF (file_hash, metadata flags, parse status)
- `transactions` — one row per extracted line item (NEW; not in TS schema)
- `analyses` — derived aggregates with `_source_ids` arrays back to transactions
- `audit_log` — every action with actor, timestamp, details

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| Backend | FastAPI + uvicorn |
| Validation | Pydantic v2 (no untyped dicts anywhere) |
| Money math | `decimal.Decimal` ALWAYS, never `float` |
| APR / IRR | `scipy.optimize.brentq` (numpy-financial is unmaintained) |
| Database | Supabase (Postgres) via supabase-py |
| LLM | Claude Sonnet 4.6 via AWS Bedrock (`AnthropicBedrock` client) |
| AWS SDK | `anthropic[bedrock]` + `boto3` |
| PDF metadata | `pikepdf` |
| Disclosure rendering | Jinja2 HTML (Tier 1 prescribed templates per state) |
| PDF generation (corpus) | `reportlab` (pure Python, programmatic, Phase 5.5) |
| Templates | Jinja2 (server-rendered HTML dashboard) |
| Interactivity | HTMX (no React, no build step) |
| Job queue | `arq` (Redis-backed, async) |
| Testing | `pytest` + `pytest-asyncio` + `hypothesis` + `pytest-snapshot` |
| Package mgmt | `uv` |

---

## Deployment target

This is the only AEGIS app — there is no other deployment to coexist with.
Deploy is fresh.

- **Host:** Single Hetzner CPX21 VM (Ubuntu 24 LTS), running both the FastAPI
  web process and the arq worker process via systemd. No Docker, no
  Kubernetes, no orchestrator. Single box because volume is ~100 deals/month.
- **Reverse proxy:** Cloudflare Tunnel (`cloudflared`, tunnel name
  `aegis-prod`) exposes **two** hostnames on the same box:
  * `aegis.commerafunding.com` → HTTPS → FastAPI on `localhost:5555`
    (the operator dashboard + bearer-token API).
  * `aegis-ssh.commerafunding.com` → SSH → `localhost:22` (operator
    deploys + ops admin via `cloudflared`-tunneled SSH).

  Both hostnames are gated by the same **Cloudflare Access** application
  ("AEGIS Dashboard") so a single SSO sign-in covers dashboard + SSH.
  The bearer token is a second layer behind Access on the API path.
  The SSH hostname is single-level (`aegis-ssh.…`, not
  `ssh.aegis.…`) because Cloudflare Universal SSL only covers
  one-level subdomains — a two-level name fails TLS handshake
  (verified 2026-05-12).
- **Process supervision:** systemd. Two units: `aegis-web.service` (uvicorn)
  and `aegis-worker.service` (arq). Both restart on failure. Both run as a
  non-root `aegis` user. Both load `/etc/aegis/aegis.env` (NOT the repo
  `.env` — separate ops-managed file with prod secrets).
- **Redis:** installed via apt, listening on localhost only, no password
  (loopback-only). For 100 deals/month a persistent Redis with
  `appendonly yes` is fine.
- **Logs:** stdout/stderr captured by `journalctl`. Application JSON logs
  also written to `/var/log/aegis/app.log` with logrotate (daily, 14 days).
  Errors duplicated to `/var/log/aegis/errors.log`.
- **Updates:** deploy is `git pull && uv sync && systemctl restart aegis-web
  aegis-worker`. No CI/CD pipeline — manual ssh + git is appropriate for a
  solo operator. `scripts/deploy.sh` runs these steps with safety checks
  (clean working tree, lockfile present, tests pass, residency env confirmed).
- **Backups:** Supabase handles Postgres backups (daily snapshots in their
  console). The Hetzner VM is treated as cattle — if it dies, rebuild from
  the deploy script. The only stateful thing on the box is Redis, and arq
  queues are recoverable (jobs lost during outage are re-uploaded by the
  operator).
- **Health:** Cloudflare hits `/healthz` every 60s. systemd hits the same
  locally for restart decisions.

---

## Non-Negotiable Rules

### Mathematical Correctness
- **NEVER use `float` for money.** Always `Decimal`. `getcontext().prec = 28`.
- **NEVER hand-roll numeric routines.** Use `scipy` for IRR / root-finding.
- **APR is computed via IRR on the actual payment stream**, never simple
  interest. Cite CA 10 CCR 950 and 12 CFR § 1026 Appendix J in code.
- **Float comparisons use explicit tolerances**, never `==`.
- **Money columns in DB are `numeric(14,2)`,** never `float8`.

### Auditability
- **Every aggregate metric stores its source transaction IDs.** A field like
  `total_deposits` exists alongside `total_deposits_source_ids: list[UUID]`.
  No exceptions.
- **Every transaction stores `source_page: int` and `source_line: int`** from
  the original PDF. The Claude extraction prompt MUST request these fields
  and the validator MUST verify they're present.
- **`audit_log` rows are written for every state change**, not just the
  high-level ones. Audit-write failures FAIL the operation, never silently
  log-and-continue (this was a bug in the TS version).

### LLM & Data Residency
- **All Claude calls go through AWS Bedrock**, never the direct Anthropic API.
  Use `AnthropicBedrock`, never `Anthropic`.
- **Use a regional inference profile** (`us.` prefix on the model ID). Bank
  statements must not transit non-US regions.
- **Boot guard:** app refuses to start unless `AEGIS_DATA_RESIDENCY_CONFIRMED=true`.
- **Pin the model ID via env var.** Migrating to a future model is a `.env`
  change, not a code change.
- **IAM scoping:** the AWS credentials must be scoped to `bedrock:InvokeModel`
  on the specific model ARN only. No `bedrock:*`.

### State Compliance (legally load-bearing)
- **NEVER add or modify a state in `compliance/states.py` without citing the
  actual statute** in a comment with a link, the bill number, the effective
  date, and the verification date.
- **The disclosure HTML for each state MUST match the template prescribed by
  that state's regulator** (CA DFPI, NY DFS, VA SCC, etc.). Generic templates
  are not acceptable. If a state's prescribed template hasn't been added,
  the disclosure endpoint MUST raise `DisclosureTemplateMissing`, never fall
  back to a generic disclosure.
- A snapshot test exists for each state's disclosure output, locking the
  format. Updating a snapshot is a deliberate decision with a comment
  explaining why.

### PII & Compliance
- **Never log PII.** PII fields are: `business_name, dba, owner_name, phone,
  email, address_*, bank_name, account_holder, account_last4, ssn, ssn_last4,
  ein, tax_id, owner_dob, transaction_description`. Logger MUST mask all of
  these by key name AND by value pattern.
- **Never store PDFs long-term.** Parse → extract → delete from disk in a
  `finally` block. Database stores transactions and metadata, not the PDF.
- **Transaction descriptions are PII.** Mask in logs. Acceptable in the
  database for funder review and audit, never in log files.

### Security
- **Never trust filenames from uploads.** On-disk name = `uuid4().hex + ".pdf"`.
  Original filename is recorded in DB but NEVER used in any path operation.
- **Always validate file size before reading into memory** (max 25 MB for PDFs).
- **All API endpoints require auth** (bearer token, constant-time compare).
  Webhooks use HMAC + timestamp freshness window (reject signatures > 5 min old).
- **No `os.system`, no `subprocess.shell=True`, no string-interpolated SQL.**
- **No secrets in code or git.** `.env` is gitignored.

### Operational Safety
- **Scripts under `scripts/audit/` write test data.** They require
  `--confirm` and refuse to write to production without
  `AEGIS_ALLOW_PRODUCTION_SEED=true` set. Never run them against
  production Supabase. The `AEGIS_ALLOW_PRODUCTION_SEED` env var
  should never be set in `/etc/aegis/aegis.env` or any committed
  file — it's a deliberate barrier.
- **Production data writes require explicit operator approval per
  action.** Earlier work seeded 4 placeholder merchants + 14 docs
  into production without explicit authorization; staff reasonably
  concluded the parser was broken (the data was). Multi-barrier
  gating now protects against repeats.

### Code Quality
- **NEVER use `Any` from typing without an explicit comment justifying it.**
  Pydantic models for all data shapes, including Claude responses.
- **`mypy --strict` must pass.** No untyped functions.
- **No new dependencies without asking.**
- **Functions over classes** unless state is genuinely needed.

---

## OPERATING PRINCIPLES — set after May 11 session

These are the durable lessons from the May 11, 2026 production debugging
+ deploy session. They override the auto-mode classifier's defaults —
the classifier is a backstop, not a license. Treat each rule as
session-start context that applies to every action in every session.

1. **Production data writes require explicit operator approval per
   action.** Reading from production Supabase, the Hetzner box, or any
   other live system is OK without per-action approval. Writing
   (`INSERT`, `UPDATE`, `DELETE`, file create/modify outside `/tmp`,
   systemd actions, `git push`) requires the operator to explicitly say
   "yes, do that specific action." The auto-mode classifier is NOT
   sufficient — if it lets a write through, that alone does not mean
   the operator authorized it. If unsure, ask first.

2. **Never claim a prior action succeeded without verification in the
   current session.** Do not say "you applied migration X earlier" or
   "we confirmed Y" unless there is visible proof in the current
   session's context. Memory across sessions is not reliable. When in
   doubt, run a verification query (or ask the operator to) and check.

3. **Never print credentials or tokens in tool output.** When
   provisioning scripts return tokens (Cloudflare API tokens, AWS keys,
   JWT secrets, bearer tokens, Supabase keys), capture them to a
   variable, write them to a gitignored file, and refer to them by
   name only in subsequent messages. Never echo their values to the
   user-visible session.

4. **Deploy uses `aegis@aegis-ssh.commerafunding.com`** via
   SSH-over-Cloudflare-Access. The `aegis` user on the box now has
   `/bin/bash`, the `aegis_ed25519` public key in
   `~aegis/.ssh/authorized_keys`, and a narrow sudoers rule allowing
   only `systemctl restart aegis-web aegis-worker`. `/opt/aegis` is
   owned by `aegis`, so `git pull` and `uv sync` preserve correct file
   ownership and don't need `sudo` for the source tree.

   Root SSH via the same key is available for ops admin (journalctl
   reads as root, sudoers edits, ssh-key rotation, ufw / cloudflared
   service changes). Direct-IP `root@5.161.51.105` with the
   `$HOME/.ssh/aegis_ed25519` key remains as the
   Access-down escape hatch but is NOT the normal path.

   `scripts/deploy.sh` defaults to `aegis@aegis-ssh.commerafunding.com`.
   Don't change the default. The pre-flight local checks
   (`make check`) need `make`, `uv`, `mypy`, `ruff`, and `pytest` on
   PATH — won't work from a Windows shell without WSL2. From a Windows
   workstation, run the remote half directly (ssh in and execute
   `git pull && uv sync && sudo systemctl restart aegis-{web,worker}`).

5. **When the operator pastes credentials into chat, flag and stop.**
   Do not continue past the leak. Tell the operator the credential
   needs rotation before any further work. Do not try to use the
   leaked credential to "move faster."

6. **Production database state must be operator-real, not seeded.**
   Do not create test merchants, test funders, or test documents in
   production Supabase. If testing is needed, use synthetic fixture
   data already in the repo or ask the operator for a test-mode
   database URL.

7. **The operator's stated state distribution matters.** When asked
   about state-specific features, ask the operator which states they
   actually fund deals in before scoping work. Do not preemptively
   expand to "all 50" or "all 44 served."

8. **Ship workflows, not aesthetics.** Default to feature work that
   closes a workflow loop (upload → parse → score → submit). Cosmetic
   refactors that ship before functional plumbing leave the operator
   no better off and they're hard to roll back later when the data
   layer has to grow to match the chrome. The v2 redesign shipped
   without the submission-CSV path and the operator's verdict was
   "this is very bad app and tool for now" — don't repeat the
   pattern.

9. **Test env vars must force-set, not setdefault.** Tests that depend
   on env vars (`API_BEARER_TOKEN`, `AEGIS_STORAGE_BACKEND`) MUST
   unconditionally write the test value in `tests/conftest.py`.
   `setdefault` is a silent no-op in any shell with
   `/etc/aegis/aegis.env` sourced — which the prod box does for
   ops-side smoke tests — and silently swaps prod creds into the
   `TestClient`, producing a fake "20 pre-existing failures" baseline
   (verified May 2026). Force-set the test values regardless of
   pre-existing env.

---

## Project Structure

```
aegis/
  pyproject.toml
  .env.example
  README.md
  CLAUDE.md               # this file
  REWRITE_PLAN.md
  COMPLIANCE.md           # statute citations for every state
  CORPUS_FINDINGS.md      # bugs surfaced by Phase 5.5 corpus + their fixes
  Makefile

  deploy/
    aegis-web.service          # systemd unit (uvicorn)
    aegis-worker.service       # systemd unit (arq)
    aegis.env.example          # full env reference for /etc/aegis/aegis.env
    install.sh                 # idempotent first-time setup on fresh Hetzner box
    cloudflared-config.yml.example
    iam-policy.json            # AWS IAM policy for the aegis-bedrock user
    logrotate.aegis            # /etc/logrotate.d/aegis config
    RUNBOOK.md                 # ops procedures (ssh, logs, rollback, key rotation)

  scripts/
    deploy.sh                       # pre-flight checks + ssh + git pull + restart + smoke
    generate_corpus.py              # synthetic statement generator (Phase 5.5, fixed seed)
    dev_seed_dashboard.py           # dev-only seed helper
    canary_upload.ps1               # operator canary upload (Windows)
    reparse_all.ps1                 # operator bulk re-parse (Windows)
    test_zoho_push.py               # one-off Zoho dry-run for ops
    cf_provision_tunnel.sh          # Cloudflare tunnel provisioning
    cf_setup_hostname.sh            # CF hostname + Access app wiring
    cf_swap_box_tunnel.sh           # cutover helper between boxes
    _debug_doc.py                   # private ops debug helpers (gitignored from
    _reparse_wipe.py                # automation contracts, called from .ps1)
    _status_probe.py
    audit/                          # gated subsystem — production-write capable
      reparse_real_pdfs.py          #   (require --confirm AND
      seed_test_funders.py          #    AEGIS_ALLOW_PRODUCTION_SEED=true)
      seed_test_merchants.py

  src/aegis/
    __init__.py
    config.py
    db.py
    llm.py                # Bedrock client + data-residency guard
    logger.py             # structured logging with PII masking
    money.py              # Decimal helpers
    core.py               # the conductor
    audit.py              # AuditLog Protocol + InMemory/Supabase impls
    storage.py            # DocumentRepository Protocol + impls

    parser/
      models.py           # Pydantic: Transaction, ExtractedStatement, etc.
      prompts.py          # Two prompts: extraction, classification
      metadata.py         # pikepdf-based tampering detection
      extract.py          # Claude pass 1: raw transaction extraction
      validate.py         # Daily reconciliation gate
      classify.py         # Claude pass 2: per-transaction classification
      patterns.py         # Deterministic fraud patterns
      aggregate.py        # Deterministic metric computation
      pipeline.py         # Orchestrator

    merchants/
      models.py           # MerchantRow Pydantic + funder-submission tracking
      repository.py       # MerchantRepository Protocol + impls

    funders/
      models.py           # FunderRow
      repository.py       # FunderRepository Protocol + impls
      extract.py          # LLM-driven guideline PDF extraction

    scoring/
      models.py
      score.py
      build_score_input.py
      match_funders.py
      submission_package.py         # build_submission_package + build_submission_files
      submission_csv.py             # funder-facing per-funder CSV (Phase 7B)
      ofac.py             # OFAC SDN sanctions check (hard decline)

    compliance/
      states.py           # Cited statute table
      apr.py              # IRR-based APR via scipy
      disclosure.py       # State-specific template router
      templates/
        ca_sb1235.html.j2 # Tier 1 — has UndefinedError gaps until
        ny_cfdl.html.j2   #   _build_context completion (deferred)
        fl_fcfdl.html.j2
        ga_sb90.html.j2

    zoho/
      client.py
      sync.py

    api/
      app.py
      auth.py
      deps.py
      routes/
        upload.py
        merchants.py
        transactions.py   # per-merchant transaction listing for audit
        deals.py
        findings.py       # MerchantFindings builder (sourced by audit CSV)
        funders.py
        disclosures.py
        webhooks_zoho.py

    web/
      router.py           # all /ui/* operator dashboard routes
      _findings_csv.py    # auditor-internal multi-section findings CSV
      _slug.py            # ASCII-safe slug helper
      _stacking_card.py   # MCA stacking summary builder
      static/
        aegis.css         # v2 design tokens + components
      templates/
        base.html.j2
        index.html.j2                 # Today dashboard (live KPIs)
        upload.html.j2
        intake.html.j2
        merchants.html.j2
        merchant_detail.html.j2       # MUST show per-aggregate drill-down
        merchant_form.html.j2
        merchant_match.html.j2        # match panel + submission form
        deals.html.j2
        funders.html.j2
        funder_detail.html.j2
        funder_import.html.j2
        funder_review.html.j2
        review.html.j2                # manual_review queue
        _compliance_ribbon.html.j2
        _score_breakdown.html.j2
        _stacking_card.html.j2
        _transactions_partial.html.j2 # HTMX drill-down partial

  tests/
    conftest.py
    fixtures/
      corpus/
        synthetic/         # generated PDFs + manifests, committed
        real/              # operator-provided PDFs (gitignored except README)
    parser/
    scoring/
    compliance/
    api/
    integration/
    snapshots/
    test_corpus.py         # Phase 5.5 corpus runner
```

---

## Working Agreement

### Before Coding
- Read `REWRITE_PLAN.md` to know which phase you're in.
- Plan before changes touching `core.py`, `parser/pipeline.py`, `scoring/`,
  or `compliance/`.

### After Coding
- `make typecheck` (mypy --strict). Must pass.
- `make test`. Must pass.
- `make lint` (ruff). Must pass.

### Stop after each Phase
- Each Phase in REWRITE_PLAN.md ends with the operator reviewing what was
  built. Stop and summarize. Do not start the next Phase without explicit
  approval.

---

## Known Gotchas

| Gotcha | Detail |
|---|---|
| Decimal context | Set `getcontext().prec = 28` at app startup |
| Decimal arithmetic | `Decimal("1.10")` works; `Decimal(1.10)` doesn't (binary float coercion) |
| Pydantic + Decimal | Use `Annotated[Decimal, Field(max_digits=14, decimal_places=2)]`. `condecimal()` is deprecated |
| pikepdf | Raises on encrypted PDFs; catch and re-raise as `PdfEncryptedError` |
| Bedrock model ID | Use `us.anthropic.claude-sonnet-4-6` (regional). The `global.` prefix routes anywhere — wrong for US data residency |
| Bedrock vs direct Anthropic | Same Messages API; only the client class and model ID differ. PDF document blocks supported on Bedrock since June 2025 |
| AnthropicBedrock auth | Reads boto3 credential chain. Don't pass keys explicitly except in tests |
| Two-pass parser | Pass 1 = transactions only, pass 2 = classification. Aggregates are deterministic, NEVER from Claude |
| Validation gate | Daily reconciliation, not just period total. Failure = manual_review, never retry |
| Source attribution | Every transaction MUST have source_page + source_line. Every aggregate MUST have _source_ids array |
| Float comparison | `abs(a - b) < Decimal("0.01")`, never `==` |

---

## Questions to Ask Before Starting

1. Is this money math? → Decimal.
2. Does this touch APR / IRR / amortization? → scipy.
3. Does this touch a state regulation? → cite the statute.
4. Does this log merchant data or transaction descriptions? → mask in logger.
5. Does this read a filename from input? → discard it, use UUID.
6. Does this produce an aggregate metric? → store its source transaction IDs.
7. Does this call Claude? → AnthropicBedrock, never the direct API.

---

**Version:** 2.0.0 (Python rewrite, two-pass + audit trail, AWS Bedrock)

## Compliance research location

Authoritative compliance research lives in `docs/compliance/`. Read
`docs/compliance/CORRECTIONS_2026-05-08.md` first for the audit log of
verification corrections. Read `docs/compliance/15_aegis_compliance_posture.md`
for the master obligation matrix.

When implementing any compliance-touching code (state regulation Tier promotion,
disclosure templates, OFAC workflow, retention logic, cybersecurity controls,
broker rules, renewal handling), cite the specific dossier and section in the
commit message. Example: `feat(compliance): promote CA from Tier 3 to Tier 1
per docs/compliance/01_california.md`.

When a dossier conflicts with the older state research notes elsewhere in the
repo, the dossier wins. The dossiers are dated 2026-05-07 with verification
pass 2026-05-08; older notes predate this research and may contain superseded
information.
