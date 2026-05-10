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

The script runs `make check` locally, ssh's in, `git pull`+`uv sync`,
restarts both units, then smoke-checks `/healthz` over the tunnel.

### Roll back to the previous SHA
```bash
ssh aegis@aegis.commerafunding.com
cd /opt/aegis
git log --oneline -n 5
git checkout <PREVIOUS_SHA>
sudo systemctl restart aegis-web aegis-worker
```
`/healthz` should return 200 within 10s.

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
