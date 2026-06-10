#!/usr/bin/env bash
# scripts/deploy.sh — push a new revision to the live Hetzner box.
#
# Pre-flight (local):
#   - working tree is clean (no uncommitted edits)
#   - uv.lock exists (deterministic dep resolution)
#   - make check passes (typecheck + lint + corpus)
#   - AEGIS_DATA_RESIDENCY_CONFIRMED=true in env (fail-closed)
#
# On-box (over ssh):
#   - git pull
#   - uv sync
#   - systemctl restart aegis-web aegis-worker
#   - smoke-check /healthz returns 200 within 10s
#
# Usage:
#   scripts/deploy.sh                            # uses env defaults
#   AEGIS_HOST=op@aegis.box scripts/deploy.sh   # override target
#
# The script aborts on the first failure. Logs land on stderr.
#
# NOTE: SSH lives on `aegis-ssh.commerafunding.com`, not `aegis.commerafunding.com`.
# The latter is the HTTPS dashboard hostname (tunnels to localhost:5555). SSH
# uses a separate one-level subdomain because Cloudflare Universal SSL only
# covers single-level names — see CLAUDE.md OP #4 and aegis_deploy_plumbing.

set -euo pipefail

AEGIS_HOST="${AEGIS_HOST:-aegis@aegis-ssh.commerafunding.com}"
AEGIS_REMOTE_PATH="${AEGIS_REMOTE_PATH:-/opt/aegis}"
AEGIS_HEALTH_URL="${AEGIS_HEALTH_URL:-http://127.0.0.1:5555/healthz}"

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

# --- pre-flight (local) ------------------------------------------------------

log "1/6  Verifying clean working tree"
if [[ -n "$(git status --porcelain)" ]]; then
  err "working tree has uncommitted changes; refusing to deploy"
fi

log "2/6  Verifying uv.lock is present"
[[ -f uv.lock ]] || err "uv.lock missing — run 'uv sync' to regenerate"

log "3/6  Verifying residency env"
if [[ "${AEGIS_DATA_RESIDENCY_CONFIRMED:-}" != "true" ]]; then
  err "AEGIS_DATA_RESIDENCY_CONFIRMED must be 'true' in the local env to deploy"
fi

log "4/6  Running make check (typecheck + lint + corpus)"
if ! make check >/tmp/aegis-deploy-check.log 2>&1; then
  tail -40 /tmp/aegis-deploy-check.log >&2
  err "make check failed — see /tmp/aegis-deploy-check.log"
fi

# --- remote pull + restart ---------------------------------------------------

log "5/6  Pulling + syncing on $AEGIS_HOST"
ssh "$AEGIS_HOST" bash -s <<EOF
set -euo pipefail
cd "$AEGIS_REMOTE_PATH"
git pull --ff-only
uv sync
sudo -n /usr/bin/systemctl restart aegis-web aegis-worker
EOF

log "6/6  Smoke-checking /healthz"
for attempt in 1 2 3 4 5; do
  if ssh "$AEGIS_HOST" "curl --fail --silent --max-time 3 $AEGIS_HEALTH_URL" >/dev/null; then
    log "deploy ok"
    exit 0
  fi
  sleep 2
done
err "healthz did not return 200 within 10s — investigate journalctl -u aegis-web"
