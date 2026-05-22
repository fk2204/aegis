# CLAUDE.md — AEGIS Working Agreement

Project-specific Claude Code instructions for AEGIS. Global rules live in `~/.claude/`; this file is AEGIS-only.

---

## Mission

AEGIS is an MCA (Merchant Cash Advance) underwriting brain for Commera Capital — Python rewrite of an earlier TS version, built for mathematical accuracy, auditability, and regulator defensibility.

- **What it does:** Parse bank statements → score deals → generate state-compliant disclosures → sync with Close CRM (webhook-driven inbound on `/webhooks/close`; operator-triggered outbound on `/deals/{id}/sync-to-close`; n8n is the planned orchestrator)
- **Scale:** Solo operator, ~100 deals/month, internal-only
- **Status:** Live deployment on Hetzner behind Cloudflare Access. To see current state, run `git log --oneline -10` and check `CORPUS_FINDINGS.md` for recent parser fixes.

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| Backend | FastAPI + uvicorn |
| Validation | Pydantic v2 (no untyped dicts) |
| Money math | `decimal.Decimal` ALWAYS, never `float` |
| APR / IRR | `scipy.optimize.brentq` |
| Database | Supabase (Postgres) via supabase-py |
| LLM | Claude Sonnet 4.6 via AWS Bedrock (`AnthropicBedrock` client) |
| PDF metadata | `pikepdf` |
| Disclosure templates | Jinja2 HTML (state-prescribed) |
| PDF generation (corpus) | `reportlab` (pure Python) |
| Dashboard | Jinja2 + HTMX (no React) |
| Job queue | `arq` (Redis-backed) |
| Testing | `pytest` + `pytest-asyncio` + `hypothesis` + `pytest-snapshot` |
| Package mgmt | `uv` |

---

## Non-Negotiable Rules (cross-cutting)

These apply to every file. Domain-specific rules (parser internals, compliance, deploy, testing) live in `.claude/rules/` and auto-load when relevant files are touched.

### Mathematical correctness
- **NEVER use `float` for money.** Always `Decimal`. `getcontext().prec = 28` at app startup.
- **NEVER hand-roll numerics.** Use `scipy` for IRR / root-finding.
- **APR via IRR on the actual payment stream**, never simple interest. Cite CA 10 CCR 950 and 12 CFR § 1026 Appendix J in code.
- **Float comparisons use explicit tolerances**: `abs(a - b) < Decimal("0.01")`, never `==`.
- **Money columns in DB are `numeric(14,2)`**, never `float8`.

### Auditability
- **Every aggregate metric stores its source transaction IDs.** A field like `total_deposits` exists alongside `total_deposits_source_ids: list[UUID]`. No exceptions.
- **Every transaction stores `source_page: int` and `source_line: int`** from the original PDF. The extraction prompt MUST request these; the validator MUST verify they're present.
- **`audit_log` rows are written for every state change.** Audit-write failures FAIL the operation, never silently log-and-continue.

### LLM & data residency
- **All Claude calls go through AWS Bedrock** via `AnthropicBedrock`, never the direct Anthropic API.
- **Use the regional inference profile** (`us.` prefix on the model ID). Bank statements must not transit non-US regions.
- **Boot guard:** app refuses to start unless `AEGIS_DATA_RESIDENCY_CONFIRMED=true`.
- **Model ID pinned via env var** — migration is a `.env` change, not a code change.
- **IAM scope:** `bedrock:InvokeModel` on the specific model ARN only. No `bedrock:*`.

### PII & data handling
- **Never log PII.** PII fields: `business_name, dba, owner_name, phone, email, address_*, bank_name, account_holder, account_last4, ssn, ssn_last4, ein, tax_id, owner_dob, transaction_description`. Logger masks by key name AND value pattern.
- **Never store PDFs long-term.** Parse → extract → delete from disk in a `finally` block. DB stores transactions and metadata, not the PDF.
- **Transaction descriptions are PII.** Mask in logs. Acceptable in the database for funder review and audit, never in log files.

### Security
- **Never trust filenames from uploads.** On-disk name = `uuid4().hex + ".pdf"`. Original filename in DB only, never in path operations.
- **Validate file size before reading into memory** (max 25 MB for PDFs).
- **All API endpoints require auth** (bearer token, constant-time compare). Webhooks use HMAC + timestamp freshness (reject sigs > 5 min old).
- **No `os.system`, no `subprocess.shell=True`, no string-interpolated SQL.**
- **No secrets in code or git.** `.env` is gitignored.

### Code quality
- **NEVER use `Any` from typing without an explicit comment justifying it.** Pydantic models for all data shapes, including Claude responses.
- **`mypy --strict` must pass.** No untyped functions.
- **No new dependencies without asking.**
- **Functions over classes** unless state is genuinely needed.

---

## Working Agreement

### Orient before coding
- Run `git log --oneline -10` to see what was done recently.
- Plan before changes touching `core.py`, `parser/pipeline.py`, `scoring/`, or `compliance/`.

### After coding
- `make typecheck` — mypy --strict, must pass
- `make test` — must pass (includes corpus by default)
- `make lint` — ruff, must pass
- `make check` runs all three

### Stop and summarize
At the end of any multi-step task, stop and summarize before moving to the next thing. Wait for explicit approval to continue.

---

## Known Gotchas

| Gotcha | Detail |
|---|---|
| Decimal context | Set `getcontext().prec = 28` at app startup |
| Decimal arithmetic | `Decimal("1.10")` works; `Decimal(1.10)` doesn't (binary float coercion) |
| Pydantic + Decimal | Use `Annotated[Decimal, Field(max_digits=14, decimal_places=2)]`. `condecimal()` is deprecated |
| pikepdf | Raises on encrypted PDFs; catch and re-raise as `PdfEncryptedError` |
| Bedrock model ID | Use `us.anthropic.claude-sonnet-4-6`. `global.` prefix routes anywhere — wrong for US data residency |
| AnthropicBedrock auth | Reads boto3 credential chain. Don't pass keys explicitly except in tests |

---

## Questions to Ask Before Starting

1. Is this money math? → Decimal.
2. Does this touch APR / IRR / amortization? → scipy.
3. Does this touch a state regulation? → cite the statute (see `rules/compliance.md`).
4. Does this log merchant data or transaction descriptions? → mask in logger.
5. Does this read a filename from input? → discard it, use UUID.
6. Does this produce an aggregate metric? → store its source transaction IDs.
7. Does this call Claude? → AnthropicBedrock, never the direct API.

---

## Where to find the rest

- **Parser architecture (two-pass flow, validation gate, aggregation rules):** `.claude/rules/architecture.md` — auto-loads when editing `src/aegis/parser/**`
- **State compliance (tier system, dossier rules, disclosure templates):** `.claude/rules/compliance.md` — auto-loads when editing `src/aegis/compliance/**` or `docs/compliance/**`
- **Deployment procedure:** `.claude/rules/deploy.md` — auto-loads when editing `deploy/**` or `scripts/deploy.sh`. Full ops procedures in `deploy/RUNBOOK.md`.
- **Testing rules:** `.claude/rules/testing.md` — auto-loads when editing `tests/**`
- **Operating principles (always-on):** `.claude/rules/operating-principles.md`
- **Compliance quick-reference:** `COMPLIANCE.md` and `docs/compliance/`
- **Corpus findings:** `CORPUS_FINDINGS.md`

---

**Last updated:** 2026-05-16