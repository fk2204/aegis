# RECOVERY — AEGIS disaster recovery procedure

Production-grade DR runbook for AEGIS. Tested quarterly per the
schedule below. The other half of this story — the daily backup
capture — lives in `scripts/backup_supabase.py` + the
`aegis-backup.{service,timer}` systemd unit.

---

## Recovery targets

| Component | RPO | RTO |
|---|---|---|
| Supabase Postgres | 24h (daily logical dump) | 2h |
| Hetzner VM       | 0 (cattle, rebuildable from repo) | 1h |
| Redis (arq queue) | 0 (in-flight jobs are recoverable from re-upload) | 5min |
| Cloudflare Tunnel | 0 (managed) | 15min for hostname re-attach |

The 24h RPO is acceptable for a ~100 deals/month operation. Tighter
recovery requires WAL shipping; see "Growth path" below.

---

## Daily backup capture

Triggered automatically by `aegis-backup.timer` at 02:00 UTC. Verify
the most recent run with:

```bash
sudo systemctl status aegis-backup.timer
sudo journalctl -u aegis-backup.service -n 50 --no-pager
```

The dump lives at `/var/lib/aegis/backups/aegis-<YYYYMMDDTHHMMSSZ>.dump`
on the Hetzner box AND at the off-Hetzner storage destination
configured via `AEGIS_BACKUP_DEST`. The off-box copy is the load-
bearing piece: if the Hetzner VM is gone, the local copy is gone too.

### Backup destinations

Set ONE of these via `/etc/aegis/aegis.env`:

| Destination | Env vars | Notes |
|---|---|---|
| AWS S3   | `AEGIS_BACKUP_DEST=s3`, `AEGIS_BACKUP_S3_URL=s3://commera-aegis-backups/` | Uses `aws` CLI; needs the AWS credentials in env or instance profile. |
| Backblaze B2 | `AEGIS_BACKUP_DEST=b2`, `AEGIS_BACKUP_B2_BUCKET=...`, `AEGIS_BACKUP_B2_PREFIX=aegis` | Uses `b2` CLI; cheaper than S3 at this scale. |
| Local disk (dev) | `AEGIS_BACKUP_DEST=local`, `AEGIS_BACKUP_LOCAL_OFFBOX=/mnt/external/aegis-backups` | Smoke-test only; do not use for prod. |

### Manual capture (operator-triggered)

If the timer missed a window or you want a pre-deploy snapshot:

```bash
sudo systemctl start aegis-backup.service
sudo journalctl -u aegis-backup.service -f
```

---

## Full recovery — Hetzner VM gone, Supabase data needs restore

Used when both halves failed: the box is unreachable AND the Supabase
project was deleted / corrupted. This is the worst case; if only one
side failed, see the partial procedures below.

### Step 1 — Provision a fresh box

1. Create a new Hetzner CPX21 (Ubuntu 24 LTS, US region for residency).
2. SSH in as root via the `aegis_ed25519` key OR re-create the key
   from the operator's local backup and place its pubkey on the new
   box's `/root/.ssh/authorized_keys`.
3. Set the hostname: `hostnamectl set-hostname aegis`.

### Step 2 — Install the application

```bash
git clone https://github.com/<your-fork>/aegis.git /opt/aegis
cd /opt/aegis
bash deploy/install.sh
```

This installs uv, system dependencies (WeasyPrint native libs +
postgres-client for pg_restore), creates the `aegis` user, and
provisions `/var/log/aegis` + `/var/lib/aegis`.

### Step 3 — Restore environment

The `/etc/aegis/aegis.env` file is NOT in the repo. Restore it from:
1. The operator's password manager (1Password vault entry "AEGIS
   prod env"), OR
2. The most recent off-box backup of `/etc/aegis/` if one exists.

Verify every var listed in `deploy/aegis.env.example` is populated.

### Step 4 — Restore Supabase

Option A — restore into the same Supabase project (the project still
exists, just the data is corrupted):

```bash
# Download the most recent dump from S3 / B2
aws s3 cp s3://commera-aegis-backups/aegis-20260518T020000Z.dump /tmp/restore.dump
# Or for B2:
# b2 download-file aegis-backups aegis/aegis-20260518T020000Z.dump /tmp/restore.dump

# Get the connection string from Supabase dashboard
#   Settings → Database → Connection string → URI (Transaction pooler)
pg_restore --no-owner --no-acl --clean --if-exists \\
    -d "postgresql://postgres:..." \\
    /tmp/restore.dump
```

Option B — restore into a new Supabase project (the old project is
gone):

1. Create a new Supabase project (must be US region for residency).
2. Run `bash deploy/install.sh`'s migration step skipped — the dump
   already contains the schema.
3. Same `pg_restore` command against the new project's DSN.
4. Update `/etc/aegis/aegis.env`'s `SUPABASE_URL` +
   `SUPABASE_SERVICE_KEY` to the new project.

### Step 5 — Point Cloudflare Tunnel at the new box

1. SSH in as root, install cloudflared.
2. Copy the existing tunnel credentials (`/etc/cloudflared/<uuid>.json`)
   from the password manager / op backup.
3. `systemctl restart cloudflared`.
4. In the Cloudflare dashboard verify
   `aegis.commerafunding.com` resolves to the tunnel and
   `aegis-ssh.commerafunding.com` is alive.

### Step 6 — Smoke test

```bash
curl -i https://aegis.commerafunding.com/healthz  # should be 200 ok
# Upload one synthetic statement (NOT a real one — per RUNBOOK §smoke test)
# Verify the parser produces output
# Verify Zoho sync completes (if configured)
```

### Step 7 — Document the recovery

Open `deploy/RECOVERY.md` (this file), append an entry to the
"Recovery log" section below:

```
- YYYY-MM-DD: Full recovery executed by <operator>. Cause: <one line>.
  Recovery RTO: <hours>. Backup dump used: aegis-YYYYMMDDTHHMMSSZ.dump.
```

---

## Partial recovery — Supabase data only

Use when the Hetzner box is healthy but Supabase data is corrupted /
deleted. Skip steps 1, 2, 5; execute step 4 (restore) and step 6
(smoke test).

## Partial recovery — Hetzner box only

Use when the box is unreachable but Supabase is fine. Execute steps
1, 2, 3, 5, 6; skip step 4 (no restore needed — the Supabase data is
already correct).

---

## Quarterly DR test

Schedule: first Monday of every quarter.

Test procedure (paper exercise, no production impact):
1. Spin up a fresh Hetzner CPX21 (cost ≈ €5 for the test day).
2. Execute steps 1-4 of the full recovery procedure against a recent
   off-box dump.
3. Run the corpus smoke against the restored Supabase project — no
   real PDFs, only fixtures from `tests/fixtures/corpus/synthetic/`.
4. Tear down the test box. Document the elapsed time + any friction
   in the "Recovery log" below.

The DR test is the source of truth for our RTO claim. If the test
takes longer than 2h end-to-end, update the RTO column above or fix
the bottleneck (typically: a missing piece of the env file).

---

## Recovery log

(Operator: append entries here when a recovery — real or test — is
executed. Each entry needs date, type, operator, outcome.)

- 2026-05-19: Phase 11 task #4 backup/DR plan documented. No
  recovery executed; documentation-only entry. First quarterly DR
  test scheduled for 2026-08-03 (first Monday of Q3 2026).

---

## Growth path: WAL shipping (RPO 1h)

Once deal volume justifies tighter than 24h RPO, switch from logical
dumps to PITR (point-in-time recovery) via Supabase's built-in
backup tier OR by shipping WAL to S3 with `wal-g`:

* Supabase: enable "Daily backups + PITR" on the project (Settings →
  Database → Backups). Built-in, no infrastructure required. Cost
  scales with project size.
* Self-managed: provision `wal-g` against the same S3 bucket as the
  logical dumps. Configure Postgres `archive_command` to stream WAL
  every 60 seconds; full base backup once a day.

Either option drops the RPO to ≤ 1h with no application-side changes.
The choice is pricing + ops-burden tradeoff; defer until volume
justifies it.
