# Cloudflare Access bypass for `/healthz`

**Status:** ops task. Not a code change. Update Cloudflare dashboard,
not the repo.

## Why

`/healthz` is hit every ~60s by Cloudflare's tunnel health checker AND
by `systemctl`'s local restart logic. Today the Access policy in front
of the `aegis.commerafunding.com` tunnel requires SSO for the entire
hostname including `/healthz`. The probe gets a 302 to the Access login
page instead of a 200, so:

- Cloudflare's tunnel-health graph shows the box as flapping even when
  the app is healthy.
- The `scripts/deploy.sh` post-restart smoke check (which hits `/healthz`
  through the public hostname) requires an Access service-token header
  rather than just curl.

## What to change

In the Cloudflare dashboard, navigate to:

  Zero Trust -> Access -> Applications -> "AEGIS Dashboard" -> Edit

In the application's path configuration, add a path exclusion (or
"Bypass" policy) for `/healthz`. The exact UI label is "Bypass policy"
under the application's policy list — add one with the path matcher
`/healthz` and the rule "Bypass" / "Everyone."

After saving, verify from outside the tunnel:

    curl -i https://aegis.commerafunding.com/healthz

The response must be `HTTP/1.1 200 OK` with the JSON body
`{"status":"ok",...}` and no `cf-access` redirect headers.

## What NOT to do

- Do **not** disable Access for the entire hostname. The dashboard
  itself and the bearer-protected API routes both still need SSO + the
  bearer token (defense in depth).
- Do **not** add `/healthz` to the bearer-token allowlist on the FastAPI
  side. The route is intentionally public on the app — the gate today
  is at the Cloudflare layer.
- Do **not** bypass `/healthz/full` (if/when it lands) — that endpoint
  is meant to surface internal status and should stay behind Access.

## Verification

- Tunnel health graph in the Cloudflare dashboard returns to a steady
  green within ~5 minutes.
- `scripts/deploy.sh`'s smoke check passes without an Access
  service-token header (the script can drop that env var).
