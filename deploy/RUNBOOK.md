# RUNBOOK — AEGIS production operations

The single source of truth for every recurring AEGIS ops procedure on
the Hetzner box. If a step fits in five minutes and the operator runs
it more than once, it's documented here.

---

## Topology

```
Internet
   │ HTTPS
Cloudflare Access (SSO gate)
   │
Cloudflare Tunnel (cloudflared)
   │
127.0.0.1:5555 (uvicorn / aegis.api.app)
   │
   ├─ AWS Bedrock (us.anthropic.claude-sonnet-4-6)   — extraction + classification
   ├─ Supabase Postgres (project tprpbomqcucuxnszeafo) — documents, transactions, analyses
   ├─ Redis (loopback, persistent)                    — arq queue
   └─ aegis-worker (arq, parse_document)
```

---

## Daily operations

### Tail web logs
```bash
journalctl -u aegis-web -f
# structured app log:
tail -f /var/log/aegis/app.log
```

### Tail worker logs
```bash
journalctl -u aegis-worker -f
```

### Restart services (no downtime)
```bash
sudo systemctl restart aegis-web aegis-worker
```
`Restart=on-failure` plus systemd's start-after-network ordering keeps
boot order correct without operator action.

### Drain the queue before maintenance
```bash
# Stop the worker; in-flight job finishes, queued jobs wait in Redis.
sudo systemctl stop aegis-worker
# When done:
sudo systemctl start aegis-worker
```

### Redis AOF persistence (one-time setup)

AEGIS uses Redis for arq queue state (parse jobs, cron schedules, webhook
circuit-breaker state). Without AOF persistence, a Redis OOM-kill or
`systemctl restart redis` drops the entire queue + cron schedule —
in-flight jobs disappear and the next firing slot resets to "whenever
the cron's normal schedule next lands."

AOF (`appendonly yes` + `appendfsync everysec`) brings the worst-case
data loss on a Redis crash down to ~1 second of writes. The cost is
trivial for AEGIS's workload (low write rate, small AOF file).

One-time setup on the prod box:

```bash
ssh -i ~/.ssh/aegis_ci_deploy root@5.161.51.105 \
  "redis-cli config set appendonly yes && \
   redis-cli config set appendfsync everysec && \
   redis-cli config rewrite"
```

`config rewrite` persists the change to `/etc/redis/redis.conf` so it
survives `systemctl restart redis` and host reboots.

Verify after the run:

```bash
redis-cli config get appendonly   # → "appendonly\nyes"
redis-cli config get appendfsync  # → "appendfsync\neverysec"
```

If a future Redis upgrade or fresh provisioning loses the setting,
re-run the same `config set` + `config rewrite` sequence — idempotent.

(Cost rationale: enabled 2026-06-27 after the cron audit discovered
the next-firing-slot reset would have been masked by Redis outages.
See `tests/workers/test_cron_registrations.py` for the registration
regression guard that catches the inverse failure mode.)

### OFAC SDN cache (one-time setup — migration 083)

The OFAC SDN screener (`aegis.compliance.ofac`) reads from a local
unified-cache JSON file built by `scripts/update_ofac_list.py`. A
systemd timer refreshes the cache daily at 05:00 UTC; without the
timer the runtime checker fails closed after 7 days (`cache_stale`)
or immediately if no cache exists (`cache_missing`).

One-time setup on the prod box (after migration 083 deploys):

```bash
ssh -i ~/.ssh/aegis_ci_deploy root@5.161.51.105 "
  # Cache directory — survives deploys, owned by aegis.
  install -d -o aegis -g aegis -m 0755 /var/lib/aegis/ofac_cache &&
  # Sync the unit files from the repo to /etc/systemd/system/.
  install -m 0644 /opt/aegis/deploy/aegis-ofac-update.service /etc/systemd/system/ &&
  install -m 0644 /opt/aegis/deploy/aegis-ofac-update.timer   /etc/systemd/system/ &&
  systemctl daemon-reload &&
  systemctl enable --now aegis-ofac-update.timer &&
  # Run the service once immediately so the dossier route has a
  # non-empty cache to screen against (otherwise every
  # ensure_ofac_check fires 'cache_missing' until 05:00 UTC tomorrow).
  systemctl start aegis-ofac-update.service &&
  echo 'OFAC cache initialised'
"
```

Verify:

```bash
ssh -i ~/.ssh/aegis_ci_deploy root@5.161.51.105 "
  systemctl status aegis-ofac-update.timer | head -5 &&
  ls -la /var/lib/aegis/ofac_cache/ofac_unified.json
"
```

If a refresh fails (network outage, OFAC site down), the script
preserves the previous merged cache so screening continues. The
checker fails closed only after the cache ages past 7 days, so the
operator has a week of grace before manual intervention is needed.

### One-time: sync `SuccessExitStatus=143` after this commit lands

The repo's `deploy/aegis-web.service` + `deploy/aegis-worker.service`
were updated to whitelist exit-code 143 (SIGTERM) so routine restarts
no longer show as `Failed with result 'exit-code'` in journalctl. Auto-
deploy syncs the repo source tree to `/opt/aegis` but does NOT re-copy
unit files into `/etc/systemd/system/` — `deploy/install.sh` is the only
path that does that, and it only runs on first-time setup. To take effect:

```bash
ssh root@5.161.51.105
install -m 0644 /opt/aegis/deploy/aegis-web.service     /etc/systemd/system/aegis-web.service
install -m 0644 /opt/aegis/deploy/aegis-worker.service  /etc/systemd/system/aegis-worker.service
systemctl daemon-reload
systemctl restart aegis-web aegis-worker
journalctl -u aegis-web -u aegis-worker -n 20 --no-pager
```

Expected: the restart line reads `Deactivated successfully` rather than
`Failed with result 'exit-code'`. One-time op; future deploys keep the
whitelist because the unit file on disk is now the new version.

---

## Deployment

Standard path (from operator's laptop, repo root):
```bash
AEGIS_DATA_RESIDENCY_CONFIRMED=true scripts/deploy.sh
```

The script runs `make check` locally, ssh's in via Cloudflare Access
(`aegis@aegis-ssh.commerafunding.com`), `git pull`+`uv sync`, restarts
both units, then smoke-checks `/healthz` over the tunnel.

**Windows note:** `scripts/deploy.sh` needs `make`, `uv`, `mypy`,
`ruff`, and `pytest` on PATH for the local pre-flight half. From a
Windows workstation, run from WSL2, or skip the script and execute the
remote half directly:
```bash
ssh aegis@aegis-ssh.commerafunding.com \
  'cd /opt/aegis && git pull --ff-only && uv sync && \
   sudo /usr/bin/systemctl restart aegis-web aegis-worker'
```

### Roll back to the previous SHA
```bash
ssh aegis@aegis-ssh.commerafunding.com
cd /opt/aegis
git log --oneline -n 5
git checkout <PREVIOUS_SHA>
sudo systemctl restart aegis-web aegis-worker
```
`/healthz` should return 200 within 10s.

---

## Database migrations

All schema changes go through `scripts/apply_migrations.py` (3C-extra runner). Never paste SQL into the Supabase dashboard. Never edit a previously-applied migration file — drift detection rejects modified files.

### Prerequisites

Populate `.env.local` (gitignored) with the Postgres URI for whichever environments you operate against. Get the URI from **Supabase dashboard → Settings → Database → Connection string → URI** (Transaction pooler, port 6543). The runner refuses to connect to a prod-tenanted URI unless `--target prod` is set explicitly.

```
MIGRATIONS_DB_URL_DEV=postgresql://...
MIGRATIONS_DB_URL_STAGING=postgresql://...
MIGRATIONS_DB_URL_PROD=postgresql://...
```

### Usage

Dry-run (preview pending migrations, touches nothing):
```bash
make migrate TARGET=prod DRY_RUN=1
```

Apply for real:
```bash
make migrate TARGET=prod
```

### What it does, in order

1. Acquires the session-scoped advisory lock `pg_try_advisory_lock(4736294826)`. Concurrent runners get `LOCK: another apply_migrations run is in progress (pid=N)` and exit 4.
2. Creates `schema_migrations` (filename, sha256, applied_at, applied_by) if absent.
3. **Bootstrap** (first run only — when `schema_migrations` is empty): probes each migration's effect (table/column existence). For each detected pre-existing migration, inserts a `schema_migrations` row with `applied_by='manual_pre_runner'`. The runner does NOT re-apply those.
4. For each migration not in `schema_migrations`: opens a single transaction, executes the migration body, inserts the `schema_migrations` row, inserts an `audit_log` row with `action='migration_applied'`, commits. On failure: rolls back the body, the schema_migrations row, and the audit row together.
5. Drift check: if any row in `schema_migrations` has a `sha256` that no longer matches the file on disk, raises `MigrationDriftError` and exits 3 without applying anything.

### Retrieve migration history

The audit row's `details` JSONB carries `filename`, `sha256`, `target`, `started_at`, `finished_at`, and `aegis_version` (short git SHA at runner invocation time). Everything beyond the migration-000 base columns lives inside `details` so the same INSERT works against pre-019 and post-019 audit_log schemas.

```sql
SELECT
  created_at,
  actor,
  details->>'aegis_version' AS aegis_version,
  details->>'filename'      AS filename,
  details->>'target'        AS target,
  details->>'sha256'        AS sha256,
  details->>'started_at'    AS started_at,
  details->>'finished_at'   AS finished_at
FROM audit_log
WHERE action = 'migration_applied'
ORDER BY created_at DESC
LIMIT 50;
```

For a count of applies per target:
```sql
SELECT details->>'target' AS target, COUNT(*)
FROM audit_log
WHERE action = 'migration_applied'
GROUP BY 1
ORDER BY 2 DESC;
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success — applied N (possibly zero) migrations. |
| 1    | Migration body raised — full rollback completed. Investigate the error and re-run. |
| 2    | Config error — missing env var, unknown `--target`, prod DSN without `--target prod`. |
| 3    | Drift — a previously-applied migration file's sha256 has changed. Restore the file (`git checkout migrations/<file>`) or roll forward via a NEW migration. Never edit applied SQL. |
| 4    | Lock held — another runner is in progress. Wait, then re-run. |

### Lock-contention recovery

The advisory lock is session-scoped — closing the runner's connection releases it automatically. If a runner crashed and the lock seems stuck:
```sql
SELECT pid, granted
FROM pg_locks
WHERE locktype='advisory' AND objid=4736294826;
-- Confirm the pid is not an active backend; terminate if needed.
SELECT pg_terminate_backend(<pid>);
```

### Adding a new migration

1. Create `migrations/NNN_short_name.sql` where NNN is the next integer (one greater than the highest existing prefix).
2. Add a probe entry to `scripts/apply_migrations.py` `MIGRATION_PROBES` (a single SELECT that returns a row iff the migration has been applied — usually a `pg_tables` / `information_schema.columns` check).
3. **Write post-apply verification checks** under `scripts/db_checks/migration-NNN-<aspect>.sql` — one file per assertion (column shape, index presence, schema_migrations row, audit_log row). Each file declares `EXPECT_ROWS: 1` so a missing row surfaces as a failed check rather than silent pass. See `scripts/db_checks/migration-032-*.sql` for the canonical four-file template.
4. Run `make migrate TARGET=dev DRY_RUN=1` to preview.
5. Run `make migrate TARGET=dev`, then the test suite, then promote to staging/prod.
6. **After every apply**, run the verification checks:
   ```bash
   uv run python scripts/db_verify.py --target prod --check migration-NNN-column
   uv run python scripts/db_verify.py --target prod --check migration-NNN-index           # if the migration added an index
   uv run python scripts/db_verify.py --target prod --check migration-NNN-schema-migrations
   uv run python scripts/db_verify.py --target prod --check migration-NNN-audit-log
   ```
   Each check exits non-zero if `EXPECT_ROWS: 1` is not satisfied. All four must pass before moving on to dependent work.

The `tests/test_apply_migrations.py::test_migration_probes_cover_every_real_migration` test fails CI if step (2) is forgotten.

---

## Verification harnesses

Two operator-zero-touch scripts replace the manual checks against prod. The operator never SSHes into Hetzner for verification and never opens the Supabase SQL editor.

### `make verify-db CHECK=<name|all> TARGET=<dev|staging|prod>`

Runs SQL from `scripts/db_checks/<name>.sql` against the DSN in `.env.local`. Connection is opened read-only; check SQL containing write keywords is refused at load. Prod DSN requires `--target prod` explicitly. The seed checks include `block-4-triggers-exist` (verifies the decisions immutability triggers from migration 015) and `triggers-immutable` (broader sibling).

Adding a new check is a one-file drop:
1. Write `scripts/db_checks/<name>.sql` — a single SELECT.
2. Optional header rows: `-- DESCRIPTION:`, `-- EXPECT_ROWS: <n>`, `-- EXPECT_ROWS_MIN: <n>`.
3. Run `make verify-db CHECK=<name> TARGET=<env>`.

### `make verify-bedrock`

scps `scripts/run_corpus_bedrock.py` + `scripts/compare_corpus_runs.py` into a fresh `/tmp/aegis-verify-<uuid>/` on the Hetzner box, runs the full corpus through real Bedrock twice (once with `AEGIS_PARSER_PAGE_ROUTING=0`, once with `=1`), then runs `compare_corpus_runs.py` to evaluate the gate. Streams output back. Returns the gate's exit code.

**Gate semantics — hard vs soft signals (post Phase 11 task #6):**

| Criterion | Type | Behavior |
|---|---|---|
| `failed_docs == 0` on baseline leg | HARD | Exit 1 if any doc fails extraction |
| `failed_docs == 0` on page-routing leg | HARD | Exit 1 if any doc fails extraction |
| clean-only TEXT-mode pct ≥ 80 on page-routing | HARD | Exit 1 if below |
| image-only VISION-mode pct == 100 on page-routing | HARD | Exit 1 if any `image_only_*` PDF mis-routes |
| image-only token reduction > 0% on page-routing | HARD | Exit 1 if vision-routed docs cost more under page-routing |
| `image_only_*` PDFs present in both legs | HARD | Exit 1 if missing — vision branch becomes unverifiable |
| clean-only token reduction > 0% | SOFT | Reported with WARN if negative; does NOT fail the run |

**History of the token-reduction criterion.** From introduction (Stage 2B) until 2026-05-19 the token-reduction signal was a hard gate. The first end-to-end verify-bedrock run on real Bedrock surfaced that the synthetic corpus was 100% text-readable: 56/56 docs passed both legs at 100% TEXT-mode pages, with a -1.18% token delta (per-page classifier overhead with no offsetting vision skip). It was demoted to a soft signal in commit `9487ea2`.

Phase 11 task #6 (this branch) added `image_only_*` synthetic PDFs under `tests/fixtures/corpus/synthetic/` that route to vision under the page router. Those PDFs give the optimization a real branch to exploit, so the token-reduction criterion is re-promoted to a hard gate against the `image_only_*` subset. The clean-only token-reduction criterion stays soft for the historical reason above — clean docs are 100% text-bearing and the optimization can't win on them.

**Operator action after merge.** The new image-only PDFs must be exercised end-to-end on real Bedrock before any deploy:

```bash
# Regenerate the image-only corpus (idempotent; produces 3 PDFs)
python -m scripts.generate_image_only_corpus

# Run the full verify-bedrock harness against the deployed corpus
make verify-bedrock
```

A successful run will show:
- `image-only VISION-mode %: 100`
- `image-only token reduction: > 0%`
- `Gate: PASS`

If verify-bedrock has not been re-run on the post-Phase-11 corpus, the gate's verdict against the vision branch is unverified.

Cleanup: on failure, `verify_bedrock.py` keeps the remote `/tmp/aegis-verify-<uuid>/` dir for forensics and prints the path. On success it removes the dir. Pass `--keep-remote` to override.

---

## Bank layout operator tools

Operator-facing flags for managing the `bank_layouts` table that feeds the Bedrock extraction prompt. Both are box-only — they require `/etc/aegis/aegis.env` to be sourced and live Supabase credentials. Dry-run by default; `--apply` to execute.

### `scripts/seed_bank_hints.py --bump-parse-count`

**When to use.** A new bank's extraction hint has just been authored in `_BANK_HINTS` (or added via a separate seed pass) but the bank's `bank_layouts.successful_parses` count is below `HINTS_AVAILABLE_THRESHOLD` (currently 3). Below the threshold the parser pipeline does NOT inject the hint into the Bedrock prompt — even if the hint text is in the row — so the bank's next statements parse without benefit. The bump is an operator-authorized backfill that lifts `successful_parses` to a target value so the hint takes effect immediately.

**Syntax (one bank per invocation):**
```bash
ssh -i ~/.ssh/aegis_ci_deploy root@5.161.51.105 \
  "cd /opt/aegis && set -a && source /etc/aegis/aegis.env && set +a && \
  .venv/bin/python scripts/seed_bank_hints.py \
  --bump-parse-count --bank-name 'Bank of America, N.A.' --target-count 3 --apply"
```

**What it does.** `UPDATE bank_layouts SET successful_parses = GREATEST(successful_parses, target_count)` for the named row, then writes one `bank_layouts.successful_parses_bumped` audit row capturing previous + new + target. `--target-count` defaults to `HINTS_AVAILABLE_THRESHOLD`. Case-insensitive `--bank-name` lookup.

**Warning.** The bumped count is a BACKFILL, NOT a real parse count. Downstream metrics that read `bank_layouts.successful_parses` to gauge bank coverage will be inflated by the operator intervention. The audit row's `note` field carries `"operator-authorized backfill — not a real parse count"` for any future analyst untangling the inflation.

**Idempotent.** If the row already meets / exceeds the target, the action is `set` with detail `"no change — successful_parses=N already ≥ target=M (GREATEST is a no-op)"` and no UPDATE issues.

### `scripts/recover_legacy_docs.py --reparse-sealed-manual-review`

**When to use.** After a `bank_layouts` hint is added for a bank whose existing documents are stuck at `parse_status='manual_review'` with a sealed `pdf_store` blob. The hint helps Bedrock extract cleanly on the next parse; this flag triggers that next parse against the already-decrypted plaintext without going through Close re-fetch.

Distinct from `--vision-retry` (which targets manual_review docs whose extraction NEVER completed — no analyses row) and `--backfill-sha-matches` (which targets manual_review docs whose pdf_store seal is MISSING). This flag fills the gap: docs with completed parses + sealed seals that just need a fresh attempt under new hint context.

**Syntax (sweeps every sealed manual_review doc):**
```bash
ssh -i ~/.ssh/aegis_ci_deploy root@5.161.51.105 \
  "cd /opt/aegis && set -a && source /etc/aegis/aegis.env && set +a && \
  .venv/bin/python scripts/recover_legacy_docs.py \
  --reparse-sealed-manual-review --apply"
```

Add `--merchant <UUID-or-lead_id-or-business-name-substring>` to scope to a single merchant for a guarded first pass.

**What it does (per candidate):**
1. `pdf_store.fetch_plaintext(doc_id)` — decrypt the sealed blob.
2. Write plaintext to a UUID-named tempfile under `aegis_upload_dir` (default `/var/lib/aegis/uploads/`).
3. `chmod 0644` the tempfile so the `aegis`-user worker can read what `root` wrote (the script SSHes in as root; the worker runs as `aegis`).
4. Enqueue `parse_document` on the arq queue with `keep_local_plaintext=False` so the worker's storage-step failure handler unlinks the tempfile instead of preserving it (the encrypted copy already exists in pdf_store; the transient plaintext should never persist past the parse).
5. 100ms `asyncio.sleep` between enqueues to pace the burst against Supabase Storage — a 26-doc burst on 2026-06-23 caused 16 `pdf_store.storage_upload_failed` events; the pacing prevents recurrence.
6. Audit row per enqueue: `document.reparse_enqueued` with the doc's filename + tempfile path.

**Cleanup.** Tempfile lifecycle is owned by the worker — `_safe_unlink` fires on every parse outcome path including the storage-step failure handlers when `keep_local_plaintext=False`. The operator does NOT need to sweep `/var/lib/aegis/uploads/` after a run.

**Outcome semantics.** A doc that comes back from the worker still in `parse_status='manual_review'` is NOT a script failure — the parser's anti-fraud + math gates intentionally hold real signal in manual_review for operator triage (`[MATH] reconciliation_failed_*`, `[META] editor_detected`, `[SHADOW] bank_statement_tampering_confirmed`, etc.). The reparse just gave the parser another chance under fresh hint context. Docs that DO transition to `proceed` / `decline` are the operator's win.

---

## Secrets + key rotation

### Rotate the API bearer token
1. Generate a new token (e.g. `openssl rand -hex 32`).
2. `sudo nano /etc/aegis/aegis.env` → set `API_BEARER_TOKEN=<new>`.
3. `sudo systemctl restart aegis-web`.
4. Update any external integration (Zoho workflow, dashboard plugin)
   that called the API with the old token.

### Rotate the Cloudflare Tunnel credentials
1. In Cloudflare dashboard → Networks → Tunnels → rotate credentials.
2. Replace `/etc/cloudflared/<tunnel-uuid>.json` with the new file.
3. `sudo systemctl restart cloudflared`.

### Rotate AWS Bedrock IAM keys
1. Issue a new access key for the `aegis-bedrock` user.
2. Edit `/etc/aegis/aegis.env` (`AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`).
3. `sudo systemctl restart aegis-web aegis-worker`.
4. Delete the old key in IAM **after** verifying parse + classify
   work end-to-end on a synthetic statement.

### Rotate the Close API key
1. In Close: Settings → Developer → API Keys → "Create new key" with
   the same role as the existing key. Note the new value (visible only
   on creation).
2. Update `CLOSE_API_KEY` in `/etc/aegis/aegis.env`.
3. `sudo systemctl restart aegis-web aegis-worker`.
4. Delete the old key in Close **after** verifying inbound webhook
   processing + outbound `/deals/{id}/sync-to-close` work end-to-end.

### Rotate the Close webhook secret
The webhook signature_key is set when the subscription is created via
`POST /api/v1/webhook/`. Rotation = delete the subscription + create
a new one (Close does not support in-place key regeneration).
1. In Close API: `DELETE /api/v1/webhook/<old_subscription_id>/`.
2. `POST /api/v1/webhook/` with the same URL + events; capture the new
   `signature_key` from the response.
3. Update `CLOSE_WEBHOOK_SECRET` in `/etc/aegis/aegis.env`.
4. `sudo systemctl restart aegis-web aegis-worker`.
5. From Close UI: move a test Opportunity to "Docs In — Pre-UW" and
   confirm the new subscription delivers (Close shows delivery
   attempts under the subscription detail page).

### Set up the Close → AEGIS callback router token

The `/api/close-callback/*` endpoints (read merchant, read deal,
trigger upload, trigger sync) authenticate via a bearer token —
same shape as `require_bearer` for the operator API but scoped to
its own env var (`CLOSE_CALLBACK_TOKEN`) so the two surfaces rotate
independently. **Run these steps in your OWN terminal — never via
the AEGIS agent's Bash tool. The plaintext token must not enter
any chat transcript.**

1. **Generate the token in your terminal:**
   ```
   openssl rand -hex 32
   ```
   The 64-hex-char output goes straight into your password manager.

2. **Paste into the env file** on the box:
   ```
   sudo nano /etc/aegis/aegis.env
   # Set: CLOSE_CALLBACK_TOKEN=<the-hex-token>
   ```
   Save, exit. The route 503s until this line is present (boot-guard
   fail-closed).

3. **Restart services:**
   ```
   sudo systemctl restart aegis-web aegis-worker
   ```
   Verify both are `active` (`systemctl is-active aegis-web aegis-worker`).

4. **Paste the same token into the Close-side trigger.** Whichever
   Close mechanism you wire — Workflow HTTP Request action, Sequence
   step, n8n flow — configure a custom header on the request:
   ```
   Authorization: Bearer <the-hex-token>
   ```
   If the Close mechanism you pick doesn't expose a way to set a
   custom header, that mechanism won't work with this route — pick
   one that does, or call the endpoints from a Close-external
   orchestrator that supports headers.

5. **Verify the route is alive** without leaving your terminal:
   ```
   ssh aegis@aegis-ssh.commerafunding.com 'curl -sS -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Bearer <the-hex-token>" \
     http://127.0.0.1:5555/api/close-callback/merchant/lead_nope'
   ```
   - **404** → token validated, lookup miss as expected. ✓ live.
   - **401** → token mismatch; re-check the env value.
   - **503** → boot guard still firing; the restart didn't pick up the env change.

6. **Log the issuance** in the rotation log below.

### Rotate the Close callback token

Generate a new value (`openssl rand -hex 32` in your terminal), update
`CLOSE_CALLBACK_TOKEN` in `/etc/aegis/aegis.env`, update the
Close-side trigger's `Authorization: Bearer` header to match, restart.

### Credential rotation log
- 2026-05-11: Cloudflare API token cfut_wKgXNs... rotated after plaintext exposure in Claude Code session. Operator deleted token in Cloudflare dashboard.

---

## Close Migration Cutover

One-time procedure for switching `/etc/aegis/aegis.env` on the Hetzner
box from the legacy Zoho integration to Close. Run when the
`feature/close-integration` branch (or its successor merge to main)
hits prod. Idempotent — re-running it is a no-op.

### Pre-cutover checklist
- [ ] Branch deploy is in flight (`git log -1` on the box shows the
      Close-integration merge commit).
- [ ] A Close API key is in hand (operator pre-generated in Close UI
      → Settings → Developer → API Keys).
- [ ] **Close webhook subscription has NOT been created yet.** The
      subscription is created AFTER `CLOSE_API_KEY` is on the box so
      the subscription POST can be issued from a working environment.
      That POST returns the `signature_key` that becomes
      `CLOSE_WEBHOOK_SECRET`. Order: deploy key → create subscription
      → drop secret → restart.

### Cutover steps

1. **Edit `/etc/aegis/aegis.env`** (root SSH or a Cloudflare-Access
   SSH session — your call):
   ```bash
   sudo $EDITOR /etc/aegis/aegis.env
   ```
   - **Drop in** `CLOSE_API_KEY=<your-key>`.
   - **Leave blank** `CLOSE_WEBHOOK_SECRET=` for now (Close gives this
     to you in the next step).
   - **Confirm** `CLOSE_DOCS_IN_PRE_UW_STATUS_ID=stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI`
     is present (the default in `config.py` is the verified live
     status id; only override if Close was renamed). Or leave the line
     out — the code default applies.
   - **Remove ALL** `ZOHO_*` lines. Every one. The boot-time residue
     warning (step 9) will yell at you in `journalctl` if any
     `ZOHO_*` survives.

2. **Restart**:
   ```bash
   sudo systemctl restart aegis-web aegis-worker
   ```

3. **Confirm no residue warning**:
   ```bash
   sudo journalctl -u aegis-web --since "1 minute ago" \
     | grep -i 'zoho_residue_detected' || echo "clean"
   ```
   If you see `config.zoho_residue_detected env_vars=[...]`, re-edit
   the env file and remove the named variables.

4. **Create the Close webhook subscription**. From any machine with
   `CLOSE_API_KEY` available (your laptop is fine — the call doesn't
   need to come from the box):
   ```bash
   curl -X POST https://api.close.com/api/v1/webhook/ \
     -u "$CLOSE_API_KEY:" \
     -H "content-type: application/json" \
     -d '{
       "url": "https://aegis.commerafunding.com/webhooks/close",
       "events": [
         {"object_type": "opportunity", "action": "updated"}
       ]
     }'
   ```
   The response includes:
   - `"id": "whsub_..."` — the subscription id (save for later
     rotation / deletion)
   - `"signature_key": "<hex>"` — this is the `CLOSE_WEBHOOK_SECRET`
     value

5. **Drop the secret**:
   ```bash
   sudo $EDITOR /etc/aegis/aegis.env
   # Set CLOSE_WEBHOOK_SECRET=<hex-from-step-4>
   sudo systemctl restart aegis-web aegis-worker
   ```

6. **Smoke test the webhook**. In Close UI: move a test Opportunity
   to "Docs In — Pre-UW". Then on the box:
   ```bash
   sudo journalctl -u aegis-web --since "30 seconds ago" \
     | grep -E 'close.webhook|close.merchant'
   ```
   Expect: one `close.webhook.received` audit row, one
   `close.merchant.created` or `close.merchant.updated` audit row.

7. **Smoke test the outbound sync** (optional but recommended). Once
   a merchant has a stored decision:
   ```bash
   curl -X POST https://aegis.commerafunding.com/deals/<merchant_uuid>/sync-to-close \
     -H "Authorization: Bearer $AEGIS_BEARER" \
     -H "Cf-Access-Authenticated-User-Email: operator@commerafunding.com"
   ```
   200 + `patched=true` on first call; 200 + `patched=false,
   reason="no_diff"` on the second.

### Post-cutover hygiene
- Delete the old Zoho refresh token + OAuth app in Zoho's developer
  console.
- Update `deploy/RUNBOOK.md` § Credential rotation log with the
  cutover date.
- The `merchants.zoho_deal_id_archived` / `zoho_lead_id_archived` DB
  columns remain (migration 026 preserved data). A future migration
  drops them when the operator certifies no audit query needs the
  archived values.

---

## Compliance + state regulation

### Add a new Tier 1 state
The audit upgrade is operator-driven — code is **never** allowed to
fill state regulation fields from prior knowledge. Workflow:

1. Operator pulls the bill text + DFPI/DFS prescribed form.
2. Hands the source material to Claude Code, which:
   - Edits `src/aegis/compliance/states.py` to upgrade the entry from
     Tier 3 to Tier 1, citing every field with a `# bill XYZ § n` comment.
   - Adds the prescribed-form template at
     `src/aegis/compliance/templates/<state>_<bill>.html.j2`.
   - Adds a snapshot test that locks the rendered HTML.
3. `make check` runs locally before deploy.

### Boot-time fail-closed
`compliance/states.validate_states_table()` runs in the FastAPI
lifespan. If a Tier 1 entry references a missing template file, the
process refuses to boot and `journalctl -u aegis-web` shows
`CompliancePolicyError`.

---

## Incidents

### Health endpoint returning 503
Most likely a stale Bedrock token + 401-loop. Check:
```bash
journalctl -u aegis-web -n 200 --no-pager | grep -i 'bedrock\|residency'
```
If `DataResidencyError`: the `BEDROCK_MODEL_ID` env var was edited to
something that does not start with `us.`. Revert the env file +
restart.

### Parser stuck on "manual_review" for healthy statements
The deterministic validator rejected the extraction. The audit log row
records the failure code:
```bash
sudo -u postgres psql -d aegis_supabase \
  -c "select created_at, action, details->'failures' from audit_log
      where action = 'document.parse.error'
      order by created_at desc limit 20;"
```
Common causes:
- `reconciliation_failed_*` — the LLM extracted rows that don't tie
  out to printed totals. Re-upload a higher-quality scan; do NOT
  loosen the validator tolerance.
- `extraction_truncated_retry_required` — the LLM hit `max_tokens`.
  Multi-page statements may need the upcoming chunked-extract
  feature; for now, send to manual review.

### Worker isn't picking up jobs
Confirm Redis is up + reachable from the worker:
```bash
sudo systemctl status redis-server
redis-cli -u redis://127.0.0.1:6379 ping     # → PONG
sudo -u aegis bash -lc 'cd /opt/aegis && uv run python -c "from arq.connections import RedisSettings; print(RedisSettings.from_dsn(\"redis://127.0.0.1:6379\"))"'
```
If those all work but the queue is full, the worker process is wedged.
`sudo systemctl restart aegis-worker` and watch
`journalctl -u aegis-worker -n 200`.

---

## System dependencies (gotchas)

- **WeasyPrint** needs Pango/GObject/Cairo/HarfBuzz native libs:
  `libpango-1.0-0`, `libpangoft2-1.0-0`, `libharfbuzz0b`, `libcairo2`,
  `libgdk-pixbuf-2.0-0`, `fonts-liberation`. `deploy/install.sh`
  installs them; re-run that script if a Tier 1 disclosure starts
  failing with a Pango import error.
- **pikepdf** raises on encrypted PDFs. The pipeline catches and
  re-raises as `PdfEncryptedError`; the worker records that as
  `document.parse.error` with code `pdf_encrypted`. Operator action:
  request an unencrypted statement.
- **`AEGIS_DATA_RESIDENCY_CONFIRMED=true`** must be set in
  `/etc/aegis/aegis.env`. Without it, `aegis.api.app` raises
  `DataResidencyError` at import and systemd loops the unit.

---

## Backups

- **Postgres:** Supabase handles daily snapshots in their console
  (under project `tprpbomqcucuxnszeafo`). Recovery point is one day;
  for tighter RPO add a logical replica.
- **Redis:** `appendonly yes` in `/etc/redis/redis.conf` so an arq
  queue replay is recoverable on restart. Worst case an outage drops
  in-flight jobs; the operator re-uploads the affected PDFs.
- **Hetzner VM:** treated as cattle. If the box dies, run the
  Cold box recovery procedure below — target wall-clock under
  30 minutes from a fresh Hetzner CPX21 to production traffic.

---

## Cold box recovery

Reproducible recipe to take a fresh Hetzner CPX21 from a clean Ubuntu
install to production traffic when the existing box is unrecoverable.
Target: under 30 minutes of operator wall-clock. Every step is a
verbatim copy-paste — no prose paraphrase, no "you know what to do."

Pre-flight assumptions: the operator holds (1) the canonical encrypted
backup of `/etc/aegis/aegis.env` in their password manager (per
`Secrets + key rotation` above), (2) the `MIGRATIONS_DB_URL_PROD` DSN
in their local `.env.local`, (3) SSH access to the cattle keypair
(`~/.ssh/aegis_ed25519`), (4) Cloudflare dashboard access for the
tunnel-credentials JSON. If any of those are missing, stop and
recover them before continuing — the recipe assumes them present.

Replace `<NEW_BOX_IP>` with the value Hetzner prints in step 1.

### 1. Provision the VM (≈3 min)

Hetzner Cloud Console → Add Server:

- **Location:** any US region (currently Ashburn or Hillsboro).
- **Image:** Ubuntu 24.04 LTS (22.04 LTS works as a fallback).
- **Type:** CPX21 (3 vCPU shared AMD / 4 GB RAM / 80 GB NVMe).
- **SSH key:** paste the public half of `~/.ssh/aegis_ed25519`.
- **Networking:** default IPv4 + IPv6.
- **Firewall:** create one allowing inbound TCP 22 from
  `0.0.0.0/0` (key-only auth is the actual gate) — the production
  HTTPS surface stays behind Cloudflare Tunnel, no direct 443 inbound.

Once the server is `running`, note the public IPv4 in the Hetzner UI.
Verify SSH:

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> 'uname -a'
```

Expected: one line including `Linux ... Ubuntu`.

### 2. Install system dependencies (≈4 min)

Run as root on the new box:

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> bash -s <<'REMOTE'
set -euo pipefail
apt-get update
apt-get install -y \
  git jq curl ca-certificates \
  python3.12 python3.12-venv python3.12-dev \
  build-essential pkg-config \
  postgresql-client \
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 \
  libgdk-pixbuf-2.0-0 fonts-liberation \
  redis-server
# uv — install for root (used during sync below) AND fetch the binary
# we'll symlink so the aegis user picks it up.
curl -LsSf https://astral.sh/uv/install.sh | sh
install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
uv --version
REMOTE
```

Expected output ends with `uv 0.x.y`.

### 3. Clone the repo to `/opt/aegis` (≈1 min)

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> bash -s <<'REMOTE'
set -euo pipefail
install -d -o root -g root -m 0755 /opt/aegis
git clone https://github.com/commerafunding/aegis.git /opt/aegis
cd /opt/aegis
git log --oneline -1
REMOTE
```

Expected: one-line `git log` output for the latest `main` commit. If
the repo is private and the box needs a deploy key, copy the same
GitHub Actions deploy key contents from the operator's password manager
into `/root/.ssh/id_ed25519` and switch the clone URL to
`git@github.com:commerafunding/aegis.git` — same key the CI workflow
uses (see `CLAUDE.md` § CI auto-deploy — operator one-time setup).

### 4. Restore `/etc/aegis/aegis.env` (≈3 min)

Decrypt the operator's canonical backup (password-manager vault entry
named **AEGIS prod /etc/aegis/aegis.env backup**) on the workstation,
then scp it to the box. The plaintext MUST NOT pass through chat
output or any log surface.

In the operator's terminal (NOT via this agent's Bash tool):

```
# decrypt to a memfile (Linux) or stdout-piped via your password
# manager's CLI, then ship straight to the box:
op read 'op://Private/AEGIS prod aegis.env backup/document' \
  | ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> \
    'install -m 0640 -o root -g root /dev/stdin /etc/aegis/aegis.env'
```

(`op` is the 1Password CLI — substitute the equivalent command for
whatever password manager the operator uses.) Verify shape WITHOUT
echoing values:

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> \
  'wc -l /etc/aegis/aegis.env && grep -cE "^[A-Z_]+=.+$" /etc/aegis/aegis.env'
```

Expected: matching line count and key=value count (no blank-line drift).

### 5. Apply migrations from the laptop (≈2 min)

Migrations run from the operator's workstation against the prod DSN —
the box deliberately doesn't carry the DB-admin DSN
(see CLAUDE.md § CI auto-deploy step 4 for the rationale). Run from
the repo root on the workstation:

```
make migrate TARGET=prod DRY_RUN=1   # preview pending migrations
make migrate TARGET=prod             # apply for real
```

Expected: `Applied 0 migrations` if Supabase persisted through the box
loss (the schema lives in Supabase, not on the box); a non-zero count
means the workstation has migrations newer than Supabase, which is
unexpected during a cold recovery — pause and inspect before
continuing.

### 6. Create the `aegis` user + take ownership (≈1 min)

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> bash -s <<'REMOTE'
set -euo pipefail
useradd -m -r -s /bin/bash aegis || true
install -d -o aegis -g aegis -m 0755 /var/log/aegis
install -d -o aegis -g aegis -m 0755 /var/lib/aegis/uploads
install -d -o aegis -g aegis -m 0755 /opt/aegis/quarantine
chown -R aegis:aegis /opt/aegis
# narrow sudoers rule (operator interactive deploys + CI both rely on this):
cat >/etc/sudoers.d/aegis-systemctl <<'SUDO'
aegis ALL=(root) NOPASSWD: /usr/bin/systemctl restart aegis-web aegis-worker
SUDO
chmod 0440 /etc/sudoers.d/aegis-systemctl
visudo -c
REMOTE
```

Expected: final line `/etc/sudoers: parsed OK`.

### 7. Install systemd units (≈1 min)

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> bash -s <<'REMOTE'
set -euo pipefail
cp /opt/aegis/deploy/aegis-web.service /etc/systemd/system/aegis-web.service
cp /opt/aegis/deploy/aegis-worker.service /etc/systemd/system/aegis-worker.service
systemctl daemon-reload
systemctl is-enabled redis-server || systemctl enable --now redis-server
redis-cli -u redis://127.0.0.1:6379 ping
REMOTE
```

Expected: `PONG` on the last line.

### 8. First-time `uv sync` as the `aegis` user (≈4 min)

The 2026-06-16 `ProtectSystem=strict` editable-install outage means
`uv sync` must run as `aegis`, not root — running as root leaves
`/opt/aegis/.venv` owned by root and `systemd`'s sandbox can't write
to it on next sync.

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> \
  'sudo -u aegis bash -lc "cd /opt/aegis && uv sync --locked"'
```

Expected: `Resolved N packages` followed by `Installed N packages`.

### 9. Start services (≈30 s)

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> bash -s <<'REMOTE'
set -euo pipefail
systemctl enable --now aegis-web aegis-worker
sleep 2
systemctl is-active aegis-web aegis-worker
REMOTE
```

Expected: two lines, both `active`. If either is `failed`, capture
`journalctl -u aegis-web -u aegis-worker --since "2 minutes ago" -p err
--no-pager` and address the root cause before continuing.

### 10. Re-point the Cloudflare Tunnel (≈4 min)

The existing tunnel `aegis-prod` keeps its DNS routes (`aegis.…` and
`aegis-ssh.…`) — the only change is which box runs `cloudflared`.
From the operator's terminal (NOT via this agent's Bash tool):

```
# 1. In Cloudflare dashboard -> Zero Trust -> Networks -> Tunnels ->
#    aegis-prod -> rotate token. Capture the new value into your
#    password manager; the prior box's connector goes stale.
# 2. Install cloudflared + register the connector on the new box:
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> bash -s <<'REMOTE'
set -euo pipefail
curl -L --output /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
apt-get install -y /tmp/cloudflared.deb
rm /tmp/cloudflared.deb
cloudflared --version
REMOTE
# 3. Pipe the token straight from your password manager into the install
#    command so it never lands in shell history or chat output:
op read 'op://Private/AEGIS prod cloudflared tunnel token/credential' \
  | ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> \
    'TOKEN=$(cat) && cloudflared service install "$TOKEN" \
       && systemctl enable --now cloudflared \
       && systemctl is-active cloudflared'
```

Expected: final output is `active`. Within ~30 s the Cloudflare
dashboard's Tunnel detail page will show the new connector as
"HEALTHY" and the old one as "INACTIVE" (which is then safe to remove
from the connector list).

> Token-handling note: `systemctl status cloudflared` leaks the
> tunnel token via argv on this install path (documented gotcha in
> `.claude/rules/deploy.md`). Use `systemctl is-active cloudflared` +
> `journalctl -u cloudflared --output=cat -n 50` for ongoing health
> checks — never `status`.

### 11. Smoke — local healthz (≈10 s)

```
ssh -i ~/.ssh/aegis_ed25519 root@<NEW_BOX_IP> \
  'curl -sS http://127.0.0.1:5555/healthz'
```

Expected:

```
{"ok":true}
```

If anything else: `journalctl -u aegis-web -n 200 --no-pager` and check
the first error. Most cold-box failures at this stage are a missing
env var in `/etc/aegis/aegis.env` (the boot-guard fail-closed prints
the exact missing key in the log line).

### 12. Smoke — external tunnel (≈10 s)

From the workstation:

```
curl -sS -o /dev/null -w "%{http_code}\n" https://aegis.commerafunding.com/healthz
```

Expected: `302` — the Cloudflare Access SSO redirect, which proves the
tunnel is up AND Cloudflare Access is gating the surface. (`200` would
mean Access is misconfigured; `502` / `504` / `530` means the tunnel
is still bootstrapping — wait 30 s and retry.)

### Post-recovery hygiene

- [ ] Log the recovery in `deploy/RUNBOOK.md` § Credential rotation log
      with the date, the recovered IP, and which credentials rotated
      (the Cloudflare tunnel token at minimum).
- [ ] Update GitHub Actions secret `AEGIS_SERVER_IP` to the new IP so
      auto-deploys continue working — see CLAUDE.md § CI auto-deploy
      step 4b.
- [ ] Authorize the CI deploy key on the new box per CLAUDE.md § CI
      auto-deploy step 2 (the cattle key works for ops, but CI uses
      a separate key in `/root/.ssh/authorized_keys`).
- [ ] Run `make verify-bedrock` from the workstation to confirm the
      vision + text-only parser legs both succeed end-to-end against
      real Bedrock.
- [ ] Decommission the dead box in the Hetzner console after 48 hours
      (long enough to copy off anything if a postmortem needs it).
