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
| PDF rendering | `weasyprint` (HTML → PDF for disclosures) |
| Templates | Jinja2 (server-rendered HTML dashboard) |
| Interactivity | HTMX (no React, no build step) |
| Job queue | `arq` (Redis-backed, async) |
| Testing | `pytest` + `pytest-asyncio` + `hypothesis` + `pytest-snapshot` |
| Package mgmt | `uv` |

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

### Code Quality
- **NEVER use `Any` from typing without an explicit comment justifying it.**
  Pydantic models for all data shapes, including Claude responses.
- **`mypy --strict` must pass.** No untyped functions.
- **No new dependencies without asking.**
- **Functions over classes** unless state is genuinely needed.

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
  Makefile

  src/aegis/
    __init__.py
    config.py
    db.py
    llm.py                # Bedrock client + data-residency guard
    logger.py             # structured logging with PII masking
    money.py              # Decimal helpers
    core.py               # the conductor

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

    scoring/
      models.py
      score.py
      build_score_input.py
      match_funders.py
      submission_package.py

    compliance/
      states.py           # Cited statute table
      apr.py              # IRR-based APR via scipy
      disclosure.py       # State-specific template router
      templates/
        ca_sb1235.html.j2
        ny_cfdl.html.j2

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
        funders.py
        disclosures.py
        webhooks_zoho.py

    web/
      app.py
      templates/
        base.html.j2
        upload.html.j2
        merchants.html.j2
        merchant_detail.html.j2  # MUST show per-aggregate drill-down

  tests/
    conftest.py
    fixtures/
    parser/
    scoring/
    compliance/
    api/
    integration/
    snapshots/
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
