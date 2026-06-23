# CLAUDE.md — AEGIS Working Agreement

Project-specific Claude Code instructions for AEGIS. Global rules live in `~/.claude/`; this file is AEGIS-only.

---

## Mission

AEGIS is an internal pre-screening tool for **Commera Capital, a pure ISO broker**. AEGIS parses merchant bank statements + processor statements, scores deal quality, captures operator override decisions, ingests funder replies, and syncs deal data with Close CRM. AEGIS **never extends financing, never generates merchant-facing disclosures, never charges merchant fees.** Funder partners own all regulator-facing compliance (CFDL disclosures, renewals, COJ / auto-debit / forum rules, §1071 reporting). AEGIS is built for mathematical accuracy and internal auditability.

- **What it does:** Parse bank statements → score deals → capture operator overrides → ingest funder replies → sync with Close CRM (webhook-driven inbound on `/webhooks/close`; operator-triggered outbound on `/deals/{id}/sync-to-close`; n8n is the planned orchestrator)
- **Scale:** Solo operator, ~100 deals/month, internal-only
- **Status:** Live deployment on Hetzner behind Cloudflare Access. Every push to `main` that passes the `test` workflow auto-deploys via `.github/workflows/deploy.yml` (Sprint 7 Track A; `uv sync --locked` + `uv run --no-sync` units, runner-side migrations, root SSH to the raw Hetzner IP). Manual `make deploy TARGET=prod` is the rollback / out-of-band escape hatch, not the primary path. To see current state, run `git log --oneline -10` and check `CORPUS_FINDINGS.md` for recent parser fixes.

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
| HTML templating | Jinja2 (dossier rendering + internal UI surfaces) |
| PDF generation (corpus) | `reportlab` (pure Python) |
| Dashboard | Jinja2 + HTMX (no React) |
| Job queue | `arq` (Redis-backed) |
| Testing | `pytest` + `pytest-asyncio` + `hypothesis` + `pytest-snapshot` |
| Package mgmt | `uv` |
| Deploy | GitHub Actions auto-deploy on push to `main` (primary). Manual `make deploy TARGET=prod` is the rollback / out-of-band fallback. |

### First-class modules under `src/aegis/`

| Module | Responsibility |
|---|---|
| `parser/` | Two-pass parse pipeline (metadata → extract → validate → classify → patterns → aggregate). See `.claude/rules/architecture.md`. |
| `scoring/` | Legacy single-axis scoring engine. Still in production under the `engine="legacy"` selector; the `fraud_score` field here is informational under `engine="track_abc"`. |
| `scoring_v2/` | Three-track redesign (Track A integrity / Track B business risk / Track C context). Pure additive scaffolding; live decline path under `engine="track_abc"`. Authoritative design: `docs/SCORING_REDESIGN_CONTINUATION.md`. |
| `counterparty/` | Per-transaction counterparty classifier (processor / own-account / international / end-customer / card-paydown / unknown). Foundation that Tracks B and C both depend on. |
| `business_intel/` | UCC filing + previous-default checker (Bedrock web search). Soft-signal layer surfaced on dossier as "Legal & UCC" chip. |
| `web_presence/` | Bedrock reputation scan — web presence quality, review signals, domain age. Soft-signal layer surfaced on dossier. |
| `bank_layouts/` | Operator-curated per-bank extraction hints. Pipeline records a layout fingerprint per parse; after a threshold of successful parses the operator can author hints that are injected into the Bedrock extraction prompt. |
| `submissions/` | Durable per-funder CSV-submission rows (one per matched funder per bundle). Live source of truth for the portfolio funder-approval panel (U20). |
| `funder_note_submissions/` | One row per "Submit to Funder" click from the dossier. Powers the dossier history block; mutated in place when the funder responds with terms. Distinct from `submissions/`. |
| `compliance/` | Internal dossier hygiene, audit-log writes, state metadata. Funder-side regulatory enforcement lives outside AEGIS. See `.claude/rules/compliance.md`. |
| `close/` | Close CRM client + field mapping + inbound webhook handler + outbound write-back. |
| `funders/` | Funder catalog, criteria storage, matching logic. |
| `deals/` | Derived merchant × document view. No `deals` table — `deal_id = "{merchant_id}:{document_id}"`. |
| `merchants/` | Merchant identity + persistence. |
| `pdf_store/` | In-Postgres AES-GCM ciphertext blob for original PDFs (migration 060). Supersedes the Supabase Storage chunk-B design. |
| `ops/` | Production observability — alerting, cost accounting, rate-limit verification. Fails open in the alerting direction. |
| `zoho/` | Legacy Zoho integration (retained for historical sync, not active for new deals). |
| `web/` | HTMX + Jinja2 operator dashboard mounted at `/ui`. |
| `api/` | FastAPI app, route registration, webhook entrypoints (`/webhooks/close`). |

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
- **Every NEW aggregate metric added going forward MUST carry source IDs at the same time the aggregate ships.** Not a follow-up commit. Not "we'll wire it next sprint." A PR that adds an aggregate without its `_source_ids` companion is incomplete. The pattern is load-bearing for the dossier drill-down ("clicking an aggregate shows the contributing transactions with page/line refs") — every gap breaks that contract.
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
- **PDF storage posture** (migration 033 / chunk A of the PDF retention redesign — see `docs/PDF_RETENTION_DESIGN.md`):
  - **Local disk:** parse → extract → delete (unchanged from day one). The worker's `_safe_unlink` MUST run only after the storage step succeeds — on storage failure the local file is preserved under `quarantine/{document_id}.pdf.enc` (ciphertext, NOT plaintext per chunk-B sub-spec) for the reconcile cron to retry. The day-one unconditional `finally`-delete is OBSOLETE with chunk B.
  - **Long-term:** encrypted ciphertext in Supabase Storage via AEGIS-managed client-side AES-256-GCM with versioned keys (`/etc/aegis/aegis.env PDF_ENCRYPTION_KEY_V{n}`). Compromised Supabase = ciphertext only. Compromised box = full disclosure — honest threat-model boundary: keys + storage creds share `aegis.env`. Mitigating box compromise requires KMS, deferred.
  - **View access:** through `GET /api/documents/{id}/original` only — SSO-authenticated, ACL-domain-gated, shared-secret tunnel header `Cf-Aegis-Tunnel-Secret` enforced. NEVER via Supabase signed URLs — `tests/test_security_invariants.py` greps source for `create_signed_url` / `get_public_url` and fails if either appears.
  - **Integrity:** SHA-256 of plaintext at `documents.sha256_original`, checked on every read (in addition to AES-GCM auth tag). Mismatch = 500 + `document.original_viewed_integrity_failed` audit row.
  - **Retention:** 7 years from upload baseline; `GREATEST(retention_until, NOW()+5yr)` on merchant soft-delete (extends, never shortens). Commera internal policy — NOT a 16 CFR §1020.220 CIP binding (AEGIS is not a covered financial institution). Nightly arq cron `run_retention_sweep_cron` enforces: blob delete → confirm absent → atomic `clear_storage_path` + `document.retention_deleted` audit with `deletion_confirmed: true`.
  - **Every PDF-touching code path writes an audit row.** Full action list in `docs/PDF_RETENTION_DESIGN.md` §12.
  - **Legacy docs** (`storage_path IS NULL`, pre-033) render without "View original PDF" link and require local re-upload for re-parse. `_reparse_*.py --from-storage` works only on post-033 docs.
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

### Scoring discipline
Learned the hard way: the H10 false-positive on VU Development (2026-06-03), the running-balance-drift catches on VU 7722 (2026-06-05), and the iText editor + reconciliation drift on A&R KM LLC's 4-of-4 Lili statements (2026-06-05). They bind every change to the scoring layer, the parser's pattern detectors, and any feature that touches the decline/approve boundary.

- **Document integrity and business risk stay separate forever.** They answer different questions ("is the statement real?" vs "can the business support repayment?") and must NOT be blended back into one tunable number. The moment they share a score, "tune the severity to clear a specific deal" becomes the path of least resistance — that's the failure mode H10 demonstrated. Track A (integrity) is a near-binary gate; Track B (business risk) is an explainable band; Track C (concentration/context) is informational and never auto-penalizes. See `docs/SCORING_REDESIGN_CONTINUATION.md` for the three-track design.
- **No track-tuning to pass a specific merchant.** Changes to severities, thresholds, escalation rules, and decline boundaries are validated against a corpus, not reverse-engineered from one deal. If a change is being proposed because "VU shouldn't decline" or "A&R KM should clear," that change does NOT ship; the rationale belongs in business-risk reasoning (Track B's band), not in the detector. A signal that fires on a merchant you believe is fine is an underwriting judgment (human review), not a reason to soften the signal. Shadow mode + corpus validation come before any decision-boundary edit; same discipline as the tampering rule.
- **Shadow-first for ALL new scoring rules — not just decision-boundary edits.** Every new detector, threshold, severity, severity-routing rule, or signal weight that COULD change a decline/approve outcome ships in shadow mode first. Shadow = logged via `audit_log` (e.g. `shadow_flags: ["new_detector_would_fire:H-NN"]`) but NOT enforced in `score_deal`. Validate against the corpus AND against a window of live shadow audit rows before flipping to live. The flip itself is a config / env var / DB flag change, not a code deploy. This is the cross-cutting reinforcement of the decision-boundary rule below: corpus-only validation isn't enough — production shadow audit data has to confirm the signal isn't firing on legitimate merchants before it gates anything.

### Decision-boundary changes — deliberate + shadow-first
Anything that moves a decline/approve boundary — a new Track A integrity detector, an updated threshold, a re-tuned severity, a parse-routing rule that changes whether a doc reaches `manual_review` vs `proceed` — runs in **shadow mode first**: it logs what it WOULD do via `audit_log`, doesn't actually do it. Validate against BOTH false positives (would it have wrongly declined?) AND true positives (does it catch the cases we built it for?) on the corpus + on live shadow audit rows before flipping to live. The flip itself is a config / env var change, not a code deploy. The tampering rule is the reference pattern.

> The retired single-axis `fraud_score` is the canonical example of why this discipline exists. H10 was a false-positive on VU Development driven by blended detectors sharing one tunable number; tuning the severity to clear one deal became the path of least resistance. The three-track redesign (`scoring_v2/`) replaced `fraud_score` with orthogonal integrity / business-risk / context outputs. `fraud_score` is informational under `engine="track_abc"` — still controls production decisions under the legacy engine selector and the parser-layer hard-decline gate (`src/aegis/parser/pipeline.py`, applied before engine selection). Do not propose changes framed against `fraud_score` going forward; the planned full retirement requires both flipping the live selector to `track_abc` AND removing the parser-layer gate, neither of which has shipped.

### Extraction & automation assists, never replaces judgment
LLM extraction (funder docs, statements, anything Bedrock-driven) is a **pre-fill assistant with human confirmation**, never autonomous creation. Proven necessary 2026-06-05: even after the funder extraction prompt was tightened, residual errors leaked at confidence 72 on Shor's ISO (agent-contract clauses misclassified as merchant-stip requirements). Auto-creating a row from extracted fields without showing the operator the editable result is banned. The pattern: extract → show in editable form with per-field confidence → operator confirms → save via the same upsert path. `/ui/funders/import` and the (planned) "add funder via Claude Code" wrapper both follow this rule; neither calls `repo.upsert()` until the operator has seen the values.

### External-integration test discipline — use real captured payloads
Tests for anything touching an external system (Close API, Supabase row shapes, Bedrock output, OFAC SDN list, Cloudflare Access headers) must validate against a **CAPTURED REAL response**, never a hand-written/synthetic fixture. Green tests against an invented shape are worthless and worse — they manufacture false confidence. Proven 2026-06-05: a synthetic Close-attachment fixture invented an `id` field that real Close attachments don't have; all 11 of the agent's tests passed against the fiction; production crashed on the first real API call with a Pydantic ValidationError. Same class of bug: mapper coverage gaps where the in-memory backend bypassed the real Supabase mappers and three field-drop bugs shipped to prod (`_row_to_document` chunk-B columns, `_row_to_deal` None-guard, `_doc_row_from_db` duplication).

When fixing an integration:
1. Capture the actual payload from the failing job (or a known-good live call).
2. **Sanitize for PII IN THE SAME STEP** — never write a raw-PII fixture to disk. The capture pipeline must run the payload through `tests/_fixture_sanitize.py::sanitize_fixture_payload` BEFORE writing the JSON. Reference template: `scripts/audit/capture_transactions_fixture.py`. Strip note bodies, email subjects, real org/user/lead ids, real merchant URL tokens, named individuals from transaction descriptions. Keep the structural key set verbatim and the absence of fields verbatim.
3. Save the sanitized payload as the fixture (`tests/<domain>/fixtures/<shape>.json`).
4. Write tests that load the fixture and verify the model + the pipeline against that exact byte sequence.
5. The PII canary (`tests/test_fixture_pii_canary.py`) runs on every CI build and fails the suite if any committed fixture has known PII patterns. Treat a canary failure as a STOP — fix the leak before pushing, never silence the test.

A green test against a fixture you wrote yourself proves your understanding matches your understanding. Only a green test against a captured payload proves your code matches reality.

**Historical mistake to avoid repeating** (2026-06-05, `ae62df2`): the foundation commit for counterparty classification shipped a fixture with named Zelle counterparty individuals because the redaction pass ran AFTER the initial fixture commit AND the regex missed the "Zelle payment to" rows. The leak ended up on `origin/main` and on GitHub. The forward fix is the sanitizer + canary above; the operator's call on the historical leak was "accept it, no force-push, redact in-tree forward". Don't ship a capture script that bypasses the sanitizer.

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
3. Does this touch a state regulation dossier? → cite the statute in the dossier (see `rules/compliance.md`). Note: state regulations no longer drive runtime broker behavior; the cite is for dossier hygiene.
4. Does this log merchant data or transaction descriptions? → mask in logger.
5. Does this read a filename from input? → discard it, use UUID.
6. Does this produce an aggregate metric? → store its source transaction IDs.
7. Does this call Claude? → AnthropicBedrock, never the direct API.

---

## CI auto-deploy — operator one-time setup

Sprint 7 Track A shipped `.github/workflows/deploy.yml`: every push to `main` that passes the `test` workflow now auto-deploys to the Hetzner box. This is the primary deploy path. `make deploy TARGET=prod` still works as the hotfix / out-of-band escape hatch and remains the rollback path (`make rollback TARGET=prod`).

Below is the one-time setup an operator runs to wire the deploy SSH key + DB DSN into GitHub Actions. Reproducible from scratch — follow top to bottom.

### 1. Generate a dedicated deploy keypair

The CI deploy key must be separate from the personal `aegis_ed25519` key used for interactive ops, so it can be rotated independently and revoked without locking the operator out.

```
ssh-keygen -t ed25519 -C "aegis-ci-deploy" -f ~/.ssh/aegis_ci_deploy
```

Leave the passphrase empty (GitHub Actions cannot supply one; the key never leaves the secret store anyway).

### 2. Authorize the public key on the prod box

Append the public key to **`root`'s** `authorized_keys` on the box. CI SSHes as root, NOT as the `aegis` user, because the routine `aegis@aegis-ssh.commerafunding.com` path resolves through Cloudflare Access — which gates the TCP handshake on an SSO session cookie GitHub-hosted runners do not have. CI therefore goes direct to the raw Hetzner public IP (see step 4b for the `AEGIS_SERVER_IP` secret + rationale), and the direct-IP path is keyed off `/root/.ssh/authorized_keys`. The narrow sudoers protection that scopes the operator's `aegis_ed25519` interactive key does NOT apply here — root has full privileges. Revocation isolation comes from rotating this CI key independently of the operator's interactive key (see § Rotation), not from a constrained sudoers rule.

Look up the box IP first (Hetzner Cloud Console, or the value you'll set as the `AEGIS_SERVER_IP` secret in step 4b); call it `BOX_IP` below.

```
ssh root@$BOX_IP 'cat >> ~/.ssh/authorized_keys' < ~/.ssh/aegis_ci_deploy.pub
```

Verify with one round-trip before moving on:

```
ssh -i ~/.ssh/aegis_ci_deploy root@$BOX_IP 'systemctl is-active aegis-web aegis-worker'
```

Expected output: two lines, both `active`.

### 3. Add the private key as a GitHub Actions secret

GitHub repo → Settings → Secrets and variables → Actions → New repository secret.

- Name: `AEGIS_DEPLOY_SSH_KEY`
- Value: full contents of `~/.ssh/aegis_ci_deploy` (begins with `-----BEGIN OPENSSH PRIVATE KEY-----`, ends with `-----END OPENSSH PRIVATE KEY-----`, trailing newline included).

```
cat ~/.ssh/aegis_ci_deploy
```

(Do not paste this into chat, only into the GitHub UI.)

### 4. Add the prod migrations DSN as a second secret

Sprint 7 chose migration option (b): migrations run on the GitHub runner, not on the box. Rationale: `/etc/aegis/aegis.env` (loaded by the systemd units, see `deploy/aegis-web.service` `EnvironmentFile=`) deliberately does NOT carry the prod DB DSN — the box only needs Supabase REST credentials, not DB-admin DSN access. The existing `scripts/deploy.sh` mirrors this posture by running `make migrate` locally, so CI does the same.

GitHub repo → Settings → Secrets and variables → Actions → New repository secret.

- Name: `MIGRATIONS_DB_URL_PROD`
- Value: the same DSN that lives in the operator's local `.env.local` under `MIGRATIONS_DB_URL_PROD`. Must contain the prod project ref `tprpbomqcucuxnszeafo` — `apply_migrations.py`'s prod guard rejects a DSN that doesn't match.

### 4b. Add the raw Hetzner IP as a third secret

The routine deploy hostname `aegis-ssh.commerafunding.com` resolves through Cloudflare Access, which gates connections on an SSO session cookie. GitHub-hosted runners do not have that cookie, so CF Access refuses the TCP handshake and `ssh-keyscan` fails with `Connection refused`. CI therefore goes direct to the Hetzner public IP, bypassing CF Access entirely.

GitHub repo → Settings → Secrets and variables → Actions → New repository secret.

- Name: `AEGIS_SERVER_IP`
- Value: the raw IPv4 address of the Hetzner box, no port, no scheme (e.g. `5.161.51.105`). Find it with `dig +short aegis-ssh.commerafunding.com @1.1.1.1` from a machine that already has CF Access cleared, or by looking at the Hetzner Cloud Console.

Make sure the Hetzner ufw allows port 22 from the GitHub Actions runner IP ranges (or `0.0.0.0/0` if the box's only inbound exposure is the key-only authorized_keys policy). The HTTPS dashboard surface stays behind CF Access — only SSH bypasses.

### 5. Confirm the auto-deploy fires on the next merge

Merge a no-op commit to `main` (e.g. a CHANGELOG line). Watch GitHub Actions:

1. `test` workflow runs and passes (~6 minutes).
2. `deploy` workflow fires automatically once `test` reports success.
3. Deploy job runs: SSH key install → known_hosts pin → on-box `git pull --ff-only` → on-box `uv sync --locked` (as the `aegis` user, fix for the 2026-06-16 `ProtectSystem=strict` editable-install outage) → migrations on the runner → on-box `sudo -n /usr/bin/systemctl restart aegis-web aegis-worker` → `/healthz` smoke (5x retry, 2s gap).

If `/healthz` does not return 200 within ~10s after restart, the job fails with an `::error::` annotation pointing at the failing step. Fall back to `make rollback TARGET=prod` from the workstation if needed.

### Required secrets — full list

| Secret | Purpose |
|---|---|
| `AEGIS_DEPLOY_SSH_KEY` | Private SSH key authorized in `/root/.ssh/authorized_keys` on the box. CI uses root because the routine `aegis@aegis-ssh.commerafunding.com` path needs a Cloudflare Access SSO cookie GitHub-hosted runners don't have; direct-IP SSH to `AEGIS_SERVER_IP` bypasses CF Access entirely. Rotate independently of the operator's interactive `aegis_ed25519` key (see § Rotation). |
| `AEGIS_SERVER_IP` | Raw Hetzner public IPv4. CI SSHes directly to this IP, bypassing Cloudflare Access (which refuses GitHub Actions runners — no SSO cookie). Interactive deploys keep using the `aegis-ssh.commerafunding.com` hostname through CF Access. |
| `MIGRATIONS_DB_URL_PROD` | Prod Supabase DSN consumed by `scripts/apply_migrations.py --target prod`. Held only by GitHub Actions secret store; never lands on the box. |

### Gotcha: `workflow_run` reads the workflow file from `main`

GitHub `workflow_run` triggers always execute the version of the workflow file that exists on the **default branch** (main), not the version on the head of the triggering ref. Practical effect:

- Edits to `.github/workflows/deploy.yml` on a feature branch DO NOT take effect for that PR's eventual merge — they take effect for the merge AFTER, once the new file is on main.
- To smoke-test changes to deploy.yml, merge them on a quiet commit and observe the deploy run on the next real commit.

This is a `workflow_run` quirk, not an AEGIS choice. Documented at https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_run.

### Rotation

The CI deploy key rotates similarly to the routine `aegis_ed25519` key, but on **root's** authorized_keys, not aegis's: generate a new pair, append the new public key to `/root/.ssh/authorized_keys` BEFORE removing the old line, update the `AEGIS_DEPLOY_SSH_KEY` secret in GitHub, then remove the old `authorized_keys` line. Use the direct-IP path (`ssh root@$BOX_IP …`) for both the append and the cleanup — CF Access on the hostname would refuse a fresh ssh-keyscan from any machine without an SSO cookie. Log the rotation in `deploy/RUNBOOK.md` § "Secrets + key rotation".

`MIGRATIONS_DB_URL_PROD` rotates with the Supabase database password — same procedure as the workstation `.env.local` rotation.

---

## Where to find the rest

- **Parser architecture (two-pass flow, validation gate, aggregation rules):** `.claude/rules/architecture.md` — auto-loads when editing `src/aegis/parser/**`
- **Internal compliance code (dossier discipline, audit-log rules, decision immutability):** `.claude/rules/compliance.md` — auto-loads when editing `src/aegis/compliance/**` or `docs/compliance/**`. Note: state CFDL disclosure / tier-routing framing is obsolete — funders own regulator-facing compliance.
- **Deployment procedure:** `.claude/rules/deploy.md` — auto-loads when editing `deploy/**` or `scripts/deploy.sh`. Full ops procedures in `deploy/RUNBOOK.md`. Primary deploy path is `.github/workflows/deploy.yml` (push to `main` → auto-deploy after `test` passes); the manual `make deploy TARGET=prod` script is the fallback / hotfix path and the rollback path uses `make rollback TARGET=prod`.
- **Testing rules:** `.claude/rules/testing.md` — auto-loads when editing `tests/**`
- **Operating principles (always-on):** `.claude/rules/operating-principles.md`
- **Compliance quick-reference:** `COMPLIANCE.md` and `docs/compliance/`
- **Corpus findings:** `CORPUS_FINDINGS.md`

---

**Last updated:** 2026-06-16 (Track C of the 3-track parallel cleanup sprint — refreshed Tech Stack with deploy posture, added a first-class-modules map under `src/aegis/` (covers `scoring_v2/`, `bank_layouts/`, `funder_note_submissions/`, `submissions/`, `pdf_store/`, `counterparty/` and the rest), promoted GitHub Actions auto-deploy to the primary deploy path with manual `make deploy TARGET=prod` as the fallback, added the "every NEW aggregate carries `_source_ids` at ship time" rule under Auditability, added the "shadow-first for ALL new scoring rules" rule under Scoring discipline, neutralized `fraud_score` framing — explicitly noted it's retired from the live decline path in favor of `scoring_v2/` Track A/B/C and is informational under `engine="track_abc"`.)

**Previous:** 2026-06-12 (Track A correctness + worker-UX + test depth wave — added box-side operations gotchas to `.claude/rules/deploy.md` covering sudo NOPASSWD literal form, systemctl-status token leak, install-script token grep, and read-rules-first; extended operating-principles Rule 4 with the funder seeding sub-rule.) (2026-06-05: Close-automation + extraction night — added external-integration test discipline, decision-boundary shadow-first, extraction-assists-not-replaces, reinforced scoring discipline with A&R KM + VU 7722 evidence.)