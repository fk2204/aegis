#!/usr/bin/env bash
# scripts/deploy.sh — push a new revision to the live Hetzner box.
#
# Sprint 5 Track A automation: the operator should never SSH for a
# routine deploy. `make deploy TARGET=prod` runs this script end-to-end
# with the standard prod host + path, including local pre-flight, the
# on-box pull, locally-driven migration apply, the on-box service
# restart, and the post-restart smoke check.
#
# Pre-flight (local):
#   - working tree is clean (no uncommitted edits)
#   - uv.lock exists (deterministic dep resolution)
#   - make check passes (typecheck + lint + corpus)
#   - AEGIS_DATA_RESIDENCY_CONFIRMED=true in env (fail-closed)
#
# On-box (over ssh, phase A — pull):
#   - sudo -n chown -R aegis:aegis /opt/aegis  (best-effort, root-only)
#   - cd /opt/aegis
#   - git pull --ff-only origin main
#
# Locally (because apply_migrations.py reads MIGRATIONS_DB_URL_PROD
# from the workstation's .env.local — the box does not have prod DB
# creds, and we want a single source of truth):
#   - make migrate TARGET=$TARGET
#
# On-box (over ssh, phase B — restart + verify):
#   - sudo -n /usr/bin/systemctl restart aegis-web aegis-worker
#     (LITERAL NOPASSWD form per .claude/rules/deploy.md — DO NOT change
#     the path or split into two calls; the box's sudoers rule matches
#     this exact argv.)
#   - systemctl is-active aegis-web aegis-worker
#     (must print "active\nactive"; failure = abort.)
#
# Smoke:
#   - curl /healthz returns 200 within 10s (existing pattern preserved)
#
# Environment:
#   - TARGET=<dev|staging|prod>  default: prod
#   - AEGIS_HOST=<user@host>     default: aegis@aegis-ssh.commerafunding.com
#   - AEGIS_REMOTE_PATH=<dir>    default: /opt/aegis
#   - AEGIS_HEALTH_URL=<url>     default: http://127.0.0.1:5555/healthz
#   - DRY_RUN=1                  print resolved commands, skip ssh + migrate
#
# Usage:
#   scripts/deploy.sh                            # full prod deploy
#   TARGET=staging scripts/deploy.sh             # staging deploy
#   DRY_RUN=1 scripts/deploy.sh                  # preview only
#
# The script aborts on the first failure. Logs land on stderr.
#
# NOTE: SSH lives on `aegis-ssh.commerafunding.com`, not
# `aegis.commerafunding.com`. The latter is the HTTPS dashboard hostname
# (tunnels to localhost:5555). SSH uses a separate one-level subdomain
# because Cloudflare Universal SSL only covers single-level names — see
# CLAUDE.md OP #4 and aegis_deploy_plumbing.

set -euo pipefail

TARGET="${TARGET:-prod}"
AEGIS_HOST="${AEGIS_HOST:-aegis@aegis-ssh.commerafunding.com}"
AEGIS_REMOTE_PATH="${AEGIS_REMOTE_PATH:-/opt/aegis}"
AEGIS_HEALTH_URL="${AEGIS_HEALTH_URL:-http://127.0.0.1:5555/healthz}"
DRY_RUN="${DRY_RUN:-0}"

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }
plan() { printf '\033[1;35m[deploy:dry-run]\033[0m %s\n' "$*" >&2; }

# --- on-box phase A: defensive chown (root-only) + pull ----------------------

# NOTE on the chown:
# Sprint 3 had a partial pull blocked because a stray file under
# /opt/aegis was root-owned (leftover from initial provisioning). We
# defensively chown back to aegis:aegis BEFORE git pull so the pull
# never trips on permissions.
#
# The box's current sudoers rule only whitelists
#   sudo -n /usr/bin/systemctl restart aegis-web aegis-worker
# so a `sudo chown` call from the `aegis` user will fail. We therefore:
#   * gate the chown on "am I running as root on the box?" (uid 0)
#   * if not root, run it via `sudo -n` and SWALLOW the failure with
#     `|| true` — chown is preventative cleanup, not a deploy gate.
# This keeps the routine `aegis@`-driven deploy unblocked while still
# benefiting if/when the script runs as root for ops admin.
REMOTE_PHASE_A=$(cat <<EOF
set -euo pipefail
if [ "\$(id -u)" = "0" ]; then
  /usr/bin/chown -R aegis:aegis "$AEGIS_REMOTE_PATH" || echo "[deploy] chown failed; continuing" >&2
else
  sudo -n /usr/bin/chown -R aegis:aegis "$AEGIS_REMOTE_PATH" 2>/dev/null || echo "[deploy] chown skipped (no sudo grant); continuing" >&2
fi
cd "$AEGIS_REMOTE_PATH"
git pull --ff-only origin main
EOF
)

# --- on-box phase B: restart + is-active verify ------------------------------

REMOTE_PHASE_B=$(cat <<EOF
set -euo pipefail
cd "$AEGIS_REMOTE_PATH"
sudo -n /usr/bin/systemctl restart aegis-web aegis-worker
state=\$(systemctl is-active aegis-web aegis-worker | tr '\n' ' ')
echo "[deploy] is-active: \$state" >&2
if ! echo "\$state" | grep -qE '^active active *$'; then
  echo "[deploy] one or both services not active after restart" >&2
  exit 1
fi
EOF
)

# --- dry-run shortcut --------------------------------------------------------

if [[ "$DRY_RUN" == "1" ]]; then
  plan "TARGET=$TARGET"
  plan "AEGIS_HOST=$AEGIS_HOST"
  plan "AEGIS_REMOTE_PATH=$AEGIS_REMOTE_PATH"
  plan "AEGIS_HEALTH_URL=$AEGIS_HEALTH_URL"
  plan ""
  plan "step 1: local pre-flight"
  plan "  git status --porcelain  (must be empty)"
  plan "  test -f uv.lock"
  plan "  test \"\$AEGIS_DATA_RESIDENCY_CONFIRMED\" = true"
  plan "  make check"
  plan ""
  plan "step 2: on-box phase A (pull) via ssh $AEGIS_HOST"
  while IFS= read -r line; do plan "  $line"; done <<<"$REMOTE_PHASE_A"
  plan ""
  plan "step 3: local migrations"
  plan "  make migrate TARGET=$TARGET"
  plan ""
  plan "step 4: on-box phase B (restart + is-active) via ssh $AEGIS_HOST"
  while IFS= read -r line; do plan "  $line"; done <<<"$REMOTE_PHASE_B"
  plan ""
  plan "step 5: smoke"
  plan "  ssh $AEGIS_HOST curl --fail --silent --max-time 3 $AEGIS_HEALTH_URL  (retry x5)"
  plan ""
  plan "dry-run complete — no ssh, no migrate, no restart executed."
  exit 0
fi

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

# --- remote pull (phase A) ---------------------------------------------------

log "5/6  On-box pull on $AEGIS_HOST"
if ! ssh "$AEGIS_HOST" bash -s <<<"$REMOTE_PHASE_A"; then
  err "on-box pull failed — investigate ssh + git on $AEGIS_HOST"
fi

# --- local migrations --------------------------------------------------------

log "5b/6 Applying pending migrations (locally driven, TARGET=$TARGET)"
if ! make migrate TARGET="$TARGET"; then
  err "make migrate TARGET=$TARGET failed — abort before restart"
fi

# --- remote restart + is-active (phase B) ------------------------------------

log "5c/6 On-box restart + is-active verify on $AEGIS_HOST"
if ! ssh "$AEGIS_HOST" bash -s <<<"$REMOTE_PHASE_B"; then
  err "on-box restart or is-active check failed — investigate journalctl -u aegis-web aegis-worker"
fi

# --- smoke -------------------------------------------------------------------

log "6/6  Smoke-checking /healthz"
for attempt in 1 2 3 4 5; do
  if ssh "$AEGIS_HOST" "curl --fail --silent --max-time 3 $AEGIS_HEALTH_URL" >/dev/null; then
    log "deploy ok"
    exit 0
  fi
  sleep 2
done
err "healthz did not return 200 within 10s — investigate journalctl -u aegis-web"
