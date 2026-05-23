# AEGIS credential rotation — 2026-05

> **Do not commit this file with real tokens filled in.** All placeholders
> (`<NEW_TOKEN>`, `<OLD_TOKEN>`, etc.) are replaced by the operator locally
> and must never be pasted into chat, git commits, or log output. See
> CLAUDE.md OP #3 and OP #5.

---

## Why

Commit `c881191` (2026-05-12) fixed a Zoho OAuth credential-leak bug in
which the refresh token was exposed via query-string parameters in HTTP
request logging; `ZOHO_REFRESH_TOKEN` must be treated as compromised. The
API bearer token and Cloudflare Access service token are rotated as
defense-in-depth because they guard every non-`/healthz` route and the SSH
path respectively, and the RUNBOOK credential-rotation log (2026-05-11)
already recorded one prior exposure incident this month.

---

## Pre-flight (read this before starting)

- [ ] Service is healthy now:
  ```bash
  curl -fsSL https://aegis.commerafunding.com/healthz
  # Expect: {"status":"ok"} with HTTP 200
  ```
- [ ] You have access to:
  - **Zoho Developer Console** — `api-console.zoho.com` (Self Client section)
  - **Cloudflare Zero Trust dashboard** — `one.dash.cloudflare.com` → Zero Trust → Access → Service Auth → Service Tokens
  - **The box** via SSH:
    ```bash
    ssh aegis@aegis-ssh.commerafunding.com 'echo connected'
    ```
- [ ] Pick a maintenance window. This procedure requires two `systemctl restart`
  calls (Steps 1 and 2). Each restart completes in under 10 seconds. There
  is no graceful drain needed for the web process during credential swaps
  (in-flight requests that reach a protected route after the env reload will
  authenticate against the new token — no request is mid-auth during a restart).
  Stop the worker before Step 2 if you want zero dropped jobs:
  ```bash
  ssh aegis@aegis-ssh.commerafunding.com 'sudo systemctl stop aegis-worker'
  # Run Step 2, then:
  ssh aegis@aegis-ssh.commerafunding.com 'sudo systemctl start aegis-worker'
  ```
- [ ] Back up the current env file before touching anything:
  ```bash
  ssh aegis@aegis-ssh.commerafunding.com \
    'sudo cp /etc/aegis/aegis.env /etc/aegis/aegis.env.bak-$(date +%Y%m%d%H%M)'
  ```
  The backup is the universal rollback for all three steps.

---

## Order of operations

1. **API bearer token** — lowest risk; internal-only; one-shot swap
2. **Zoho refresh token** — medium risk; affects CRM sync only; service restart required
3. **Cloudflare Access service token** — highest risk; gates every dashboard login and the SSH path; allow 24 h overlap before revoking

---

## Step 1 — API bearer token

### Why this step is one-shot (no dual-accept window)

`auth.py` reads `API_BEARER_TOKEN` via `get_settings()` on every request
using a single `hmac.compare_digest` check against one value. There is no
multi-value list or secondary-token field. The moment the env var is
replaced and the service restarts, the old token is invalid. Any caller
(operator scripts, `test_zoho_push.py`) must be updated to the new token
before the service restarts.

### Identify callers before proceeding

The bearer token is currently used in:
- `scripts/test_zoho_push.py` (requires `API_BEARER_TOKEN` env var locally)
- `scripts/canary_upload.ps1` (operator script)
- `scripts/reparse_all.ps1` (operator script)
- Any other operator tooling that calls protected `/deals/*`, `/merchants/*`,
  `/uploads/*` routes

Update all callers with the new token **before** restarting the service.

### Generate

On your local machine (never on the box, never in chat):

```bash
openssl rand -hex 32
# Save the output to your password manager as "AEGIS API_BEARER_TOKEN 2026-05"
# Label the old one "AEGIS API_BEARER_TOKEN 2026-05 (RETIRED — rotate after c881191)"
```

### Stage on box

```bash
ssh aegis@aegis-ssh.commerafunding.com

# Edit the env file
sudo nano /etc/aegis/aegis.env
# Find:  API_BEARER_TOKEN=<OLD_TOKEN>
# Replace with:  API_BEARER_TOKEN=<NEW_TOKEN>
# Write and close (Ctrl+O, Enter, Ctrl+X)
```

### Update callers (before restart)

On your local machine, update any script or shell session that exports
`API_BEARER_TOKEN` to the new value. Do not paste the token into this chat.

### Restart and verify

```bash
# Restart from the box session (already SSH'd in):
sudo systemctl restart aegis-web

# From your local machine — confirm new token is accepted:
curl -fsSL \
  -H "Authorization: Bearer <NEW_TOKEN>" \
  https://aegis.commerafunding.com/merchants
# Expect: JSON array (even if empty), HTTP 200

# Confirm old token is now rejected:
curl -i \
  -H "Authorization: Bearer <OLD_TOKEN>" \
  https://aegis.commerafunding.com/merchants
# Expect: HTTP 401  {"detail":"Invalid bearer token"}
```

### Rollback

If the service fails to start or the new token is rejected:

```bash
ssh aegis@aegis-ssh.commerafunding.com
sudo cp /etc/aegis/aegis.env.bak-<TIMESTAMP> /etc/aegis/aegis.env
sudo systemctl restart aegis-web
curl -fsSL https://aegis.commerafunding.com/healthz
```

---

## Step 2 — Zoho refresh token (RETIRED — Zoho integration removed)

The Zoho integration was retired during the 2026-05-22 Close-CRM
cutover. The original Step 2 procedure (revoke + re-grant + exchange
for refresh token, then update `ZOHO_REFRESH_TOKEN` in
`/etc/aegis/aegis.env`) no longer applies — AEGIS no longer talks to
Zoho.

For the Close-side equivalents, see `deploy/RUNBOOK.md`:

  * **§ Rotate the Close API key** — create new key in Close →
    update `CLOSE_API_KEY` → restart → delete old key.
  * **§ Rotate the Close webhook secret** — `DELETE` + recreate the
    webhook subscription via the Close API (Close doesn't support
    in-place `signature_key` regeneration); capture the new
    `signature_key` from the POST response → update
    `CLOSE_WEBHOOK_SECRET` → restart.

The full Close migration cutover procedure (`/etc/aegis/aegis.env`
edit, subscription creation, smoke tests) lives in
`deploy/RUNBOOK.md` § Close Migration Cutover.

---

## Step 3 — Cloudflare Access service token (Dashboard app)

### Architecture note

Both `aegis.commerafunding.com` (dashboard + API) and
`aegis-ssh.commerafunding.com` (SSH) are gated by the same Cloudflare
Access application ("AEGIS Dashboard", app ID
`2a4bbc73-8d8f-460f-ab11-b60bbb81354e`). A service token is the
machine-credential form of Access authentication (used by operator
scripts, CI, or `cloudflared` SSH tunnels that can't do SSO
interactively).

If you are using **browser SSO only** (human operator, no scripts), you
may not have an active service token and can skip Step 3. Proceed only
if you have a service token in use.

### Allow 24 h overlap — do not revoke immediately

Cloudflare Access JWTs have a default TTL of 24 hours. Clients that
hold a cached JWT will continue to authenticate against the old service
token until their JWT expires. Revoking the old token immediately causes
intermittent 403s for those clients. Add the new token first, wait 24 h,
then revoke the old one.

### Generate new service token

1. Go to `https://one.dash.cloudflare.com` → **Zero Trust** → **Access**
   → **Service Auth** → **Service Tokens**.
2. Find the token associated with the "AEGIS Dashboard" app (or the one
   you actively use).
3. Click **Create Service Token** (do not delete the old one yet).
4. Copy the **Client ID** and **Client Secret** — these are shown only
   once. Save to your password manager.

### Update the token wherever it is used

Common locations for service token credentials:
- Operator workstation: `~/.cloudflared/` config or env exports
- Any CI environment variables
- The `cloudflared` client config on the box
  (`/etc/cloudflared/` or the systemd unit's environment)

Update all of these to the new **Client ID** and **Client Secret** before
proceeding. Test with:

```bash
# If your tooling exports CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
CF_ACCESS_CLIENT_ID=<NEW_CLIENT_ID> \
CF_ACCESS_CLIENT_SECRET=<NEW_CLIENT_SECRET> \
ssh aegis@aegis-ssh.commerafunding.com 'echo connected'
# Expect: "connected"
```

### Wait 24 h, then revoke the old token

After confirming the new token works and 24 h have passed:

1. Return to **Zero Trust** → **Access** → **Service Auth** → **Service Tokens**.
2. Find the old token entry.
3. Click **Revoke** (or **Delete** — Cloudflare's UI labels vary).

### Rollback

Service tokens are independent credentials — having two simultaneously
active is safe. If the new token does not work, revert your local config
to the old Client ID + Client Secret. The old token remains valid until
you explicitly revoke it.

---

## Post-rotation verification

Run all of these before closing the maintenance window:

- [ ] Healthz returns 200:
  ```bash
  curl -fsSL https://aegis.commerafunding.com/healthz
  # {"status":"ok"}
  ```
- [ ] SSH path works:
  ```bash
  ssh aegis@aegis-ssh.commerafunding.com 'echo ok'
  # ok
  ```
- [ ] Dashboard loads in browser (Access SSO redirects, no 403)
- [ ] One end-to-end deal smoke test:
  - Upload a statement PDF via the dashboard **or** run `scripts/test_zoho_push.py`
  - Confirm the deal syncs to Zoho without errors
  - Check `journalctl -u aegis-web -n 100` for `ZohoAuthError`, `Invalid bearer
    token`, or `ZohoError` lines — expect zero

---

## Credential rotation log entry

After completing rotation, append a line to the log in `RUNBOOK.md`
under **Credential rotation log**:

```
- 2026-05-XX: c881191 post-leak rotation — API_BEARER_TOKEN, ZOHO_REFRESH_TOKEN,
  Cloudflare Access service token. Operator: <initials>.
```

---

## When you can mark complete

- [ ] 24 h have passed since all three rotations without error spikes in
  `journalctl -u aegis-web` or `journalctl -u aegis-worker`
- [ ] No `ZohoAuthError`, `Invalid bearer token`, or Cloudflare 403 entries
  in `/var/log/aegis/errors.log`
- [ ] Operator confirms at least one real deal (not a fixture) flowed
  through the full pipeline (upload → parse → score → Zoho sync) after
  rotation
