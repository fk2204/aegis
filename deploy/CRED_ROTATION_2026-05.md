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

## Step 2 — Zoho refresh token

### What failure looks like during rotation

`client.py` calls `ZohoTokenCache.get()`, which POSTs to
`/oauth/v2/token` with the refresh token. On HTTP 4xx from Zoho (bad
token, revoked), it raises `ZohoAuthError: token refresh failed: ...`.

The `ZohoClient.request()` method retries once on a 401 by calling
`_auth_headers(force_refresh=True)`. If the force-refresh also returns a
bad token (because `ZOHO_REFRESH_TOKEN` in env is already wrong), a second
`ZohoAuthError` is raised and `tenacity` re-raises after 3 attempts
(~5 s total). The sync route returns HTTP 500 to the dashboard; the
operator sees a red sync-failed banner. No data is lost — the deal
record in AEGIS is unchanged; only the Zoho push failed.

Verdict: failure is **loud and recoverable**, not silent.

### Determine current scopes

Open Zoho Developer Console at `https://api-console.zoho.com`.
Click **Self Client** → find the existing AEGIS client (identified by
`ZOHO_CLIENT_ID`). Note the scopes that were granted. The minimum
required scopes for AEGIS to function are:

| Scope | Used for |
|---|---|
| `ZohoCRM.modules.Deals.ALL` | Upsert deals, read deal descriptions |
| `ZohoCRM.modules.Leads.ALL` | Upsert leads, email matchback |
| `ZohoCRM.modules.Lenders.READ` | Lender counter lookups |
| `ZohoCRM.modules.Lenders.WRITE` | Bump Total_Submissions |
| `ZohoCRM.modules.Attachments.CREATE` | Upload findings CSV + submission ZIP |
| `ZohoCRM.modules.Leads.SEARCH` | Email matchback search |

> If your existing token was granted broader scopes (e.g.
> `ZohoCRM.modules.ALL`), that also covers the above. Re-grant whatever
> you had. Do not narrow scope during this rotation.

WorkDrive scopes (`WorkDrive.files.CREATE`, `WorkDrive.files.READ`, etc.)
were referenced in earlier planning notes but are **not used** by current
code (`sync.py` only calls CRM APIs + `upload_attachment` on CRM records).
Do not add WorkDrive scopes unless a future phase explicitly requires them.

### Revoke the existing token

1. Go to `https://api-console.zoho.com` → **Self Client**
2. Find the AEGIS self-client entry.
3. Click **Revoke Token** (or navigate to the token list and revoke
   the current refresh token). This immediately invalidates the old
   `ZOHO_REFRESH_TOKEN`.

> After this point, CRM syncs will fail until Step 2 is complete.
> Keep the maintenance window short.

### Generate a new grant code

Still in **Self Client**:

1. Click **Generate Code**.
2. Enter the scopes listed in the table above (comma-separated).
3. Set **Time Duration** to whatever the console offers (typically 1 minute
   — you must exchange it within that window).
4. Click **Create** and copy the **Grant Token** (one-time use code).

### Exchange for a refresh token

On your local machine — do this immediately after copying the grant code:

```bash
curl -X POST "https://accounts.zoho.com/oauth/v2/token" \
  -d "code=<GRANT_CODE>" \
  -d "client_id=<ZOHO_CLIENT_ID>" \
  -d "client_secret=<ZOHO_CLIENT_SECRET>" \
  -d "redirect_uri=https://www.zoho.com" \
  -d "grant_type=authorization_code"
# Response includes:  "refresh_token": "<NEW_REFRESH_TOKEN>"
# Save <NEW_REFRESH_TOKEN> to your password manager.
# Do not paste it into chat.
```

Note: `redirect_uri=https://www.zoho.com` is the standard Self Client
redirect URI; if you used a custom URI when the client was originally
created, use that instead.

### Update env and restart

```bash
ssh aegis@aegis-ssh.commerafunding.com

sudo nano /etc/aegis/aegis.env
# Find:   ZOHO_REFRESH_TOKEN=<OLD_REFRESH_TOKEN>
# Replace with:   ZOHO_REFRESH_TOKEN=<NEW_REFRESH_TOKEN>
# Write and close.

sudo systemctl restart aegis-web aegis-worker
```

### Verify

Use the smoke-test script from your local machine. It requires a valid
Zoho access token (exchange via the new refresh token first) and a real
merchant UUID that already exists in Supabase:

```bash
AEGIS_BASE_URL=https://aegis.commerafunding.com \
API_BEARER_TOKEN=<NEW_API_BEARER_TOKEN> \
ZOHO_ACCESS_TOKEN=<SHORT_LIVED_ACCESS_TOKEN> \
python scripts/test_zoho_push.py <MERCHANT_UUID>
# Expect: "Smoke test passed." with all three steps green
```

To get a short-lived access token for the script's Step 3 Zoho GET:

```bash
curl -X POST "https://accounts.zoho.com/oauth/v2/token" \
  -d "refresh_token=<NEW_REFRESH_TOKEN>" \
  -d "client_id=<ZOHO_CLIENT_ID>" \
  -d "client_secret=<ZOHO_CLIENT_SECRET>" \
  -d "grant_type=refresh_token"
# "access_token" in the response is valid for ~1 hour
```

Alternatively, trigger a real sync from the AEGIS dashboard for any
existing merchant. Watch `journalctl -u aegis-web -f` on the box; a
successful sync logs `zoho.deal.upsert` or `zoho.lead.upsert` at INFO with
no `ZohoAuthError` lines.

### Rollback

If the service is in a `ZohoAuthError` loop:

```bash
ssh aegis@aegis-ssh.commerafunding.com
sudo cp /etc/aegis/aegis.env.bak-<TIMESTAMP> /etc/aegis/aegis.env
sudo systemctl restart aegis-web aegis-worker
```

The old refresh token was revoked in Zoho, so restoring the backup will
not make CRM syncs work again — but it will restore the service to a
stable state. You then need to re-generate a new grant code and try again.

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
