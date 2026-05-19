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
3. Run `make migrate TARGET=dev DRY_RUN=1` to preview.
4. Run `make migrate TARGET=dev`, then the test suite, then promote to staging/prod.

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

**Gate semantics — hard vs soft signals:**

| Criterion | Type | Behavior |
|---|---|---|
| `failed_docs == 0` on baseline leg | HARD | Exit 1 if any doc fails extraction |
| `failed_docs == 0` on page-routing leg | HARD | Exit 1 if any doc fails extraction |
| clean-only TEXT-mode pct ≥ 80 on page-routing | HARD | Exit 1 if below |
| clean-only token reduction > 0% | SOFT | Reported with WARN if negative; does NOT fail the run |

**Why token reduction is currently a soft signal:** the page-routing optimization saves tokens only when a PDF has a mix of text-readable and image-only pages (text pages skip the expensive vision pass). The current synthetic corpus (`tests/fixtures/corpus/synthetic/`) is 100% text-readable, so the optimization adds the per-page classifier's fixed overhead without ever exploiting the vision-vs-text branch. Verified on 2026-05-19: 56/56 docs passed both legs at 100% TEXT-mode pages, with a -1.18% token delta (classifier overhead). The signal will become meaningful — and re-promotable to a hard gate — once the corpus includes image-only / scanned-style PDFs. Tracked under Phase 11 task #6 in `docs/AEGIS_MASTER_PLAN.md`.

Cleanup: on failure, `verify_bedrock.py` keeps the remote `/tmp/aegis-verify-<uuid>/` dir for forensics and prints the path. On success it removes the dir. Pass `--keep-remote` to override.

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

### Rotate Zoho refresh token
1. Re-authorize the app in Zoho's developer console; grab the new
   `refresh_token`.
2. Update `ZOHO_REFRESH_TOKEN` in `/etc/aegis/aegis.env`.
3. `sudo systemctl restart aegis-web aegis-worker`.

### Credential rotation log
- 2026-05-11: Cloudflare API token cfut_wKgXNs... rotated after plaintext exposure in Claude Code session. Operator deleted token in Cloudflare dashboard.

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
- **Hetzner VM:** treated as cattle. If the box dies, run
  `deploy/install.sh` on a fresh CPX21 from the repo, point Cloudflare
  Tunnel at the new IP, populate `/etc/aegis/aegis.env`, restart.
