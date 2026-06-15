#!/usr/bin/env bash
# scripts/rollback.sh — revert one commit on the live Hetzner box.
#
# Sprint 5 Track A safety net: if a deploy went bad, the operator runs
#   make rollback TARGET=prod
# and this script reverts /opt/aegis to HEAD~1, restarts services, and
# smoke-checks. Same SSH host + path resolution as scripts/deploy.sh.
#
# IMPORTANT: this script runs `git reset --hard HEAD~1` ON THE BOX (over
# ssh), not on the workstation. The operator's local working tree is
# never touched. The reset is bounded to the box's /opt/aegis checkout,
# which is downstream of origin/main — the operator's next deploy will
# pull whatever is on origin/main again, so rollback is "undo on the
# box," not "rewrite history."
#
# Sequence (all on-box, single ssh session):
#   - cd /opt/aegis
#   - git reset --hard HEAD~1
#   - sudo -n /usr/bin/systemctl restart aegis-web aegis-worker
#     (literal NOPASSWD form per .claude/rules/deploy.md — DO NOT change.)
#   - systemctl is-active aegis-web aegis-worker  (must be active active)
# Then locally:
#   - smoke /healthz via ssh
#
# Environment:
#   - TARGET=<dev|staging|prod>  default: prod  (advisory; same script for all)
#   - AEGIS_HOST=<user@host>     default: aegis@aegis-ssh.commerafunding.com
#   - AEGIS_REMOTE_PATH=<dir>    default: /opt/aegis
#   - AEGIS_HEALTH_URL=<url>     default: http://127.0.0.1:5555/healthz
#   - DRY_RUN=1                  print resolved commands, skip ssh
#
# Usage:
#   scripts/rollback.sh                       # full prod rollback
#   DRY_RUN=1 scripts/rollback.sh             # preview only
#
# Fail-fast: any non-zero step aborts the script with a non-zero exit.

set -euo pipefail

TARGET="${TARGET:-prod}"
AEGIS_HOST="${AEGIS_HOST:-aegis@aegis-ssh.commerafunding.com}"
AEGIS_REMOTE_PATH="${AEGIS_REMOTE_PATH:-/opt/aegis}"
AEGIS_HEALTH_URL="${AEGIS_HEALTH_URL:-http://127.0.0.1:5555/healthz}"
DRY_RUN="${DRY_RUN:-0}"

log()  { printf '\033[1;34m[rollback]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[rollback]\033[0m %s\n' "$*" >&2; exit 1; }
plan() { printf '\033[1;35m[rollback:dry-run]\033[0m %s\n' "$*" >&2; }

# --- single on-box block: reset + restart + is-active ------------------------

REMOTE_BLOCK=$(cat <<EOF
set -euo pipefail
cd "$AEGIS_REMOTE_PATH"
git reset --hard HEAD~1
sudo -n /usr/bin/systemctl restart aegis-web aegis-worker
state=\$(systemctl is-active aegis-web aegis-worker | tr '\n' ' ')
echo "[rollback] is-active: \$state" >&2
if ! echo "\$state" | grep -qE '^active active *$'; then
  echo "[rollback] one or both services not active after restart" >&2
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
  plan "step 1: on-box rollback + restart + is-active via ssh $AEGIS_HOST"
  while IFS= read -r line; do plan "  $line"; done <<<"$REMOTE_BLOCK"
  plan ""
  plan "step 2: smoke"
  plan "  ssh $AEGIS_HOST curl --fail --silent --max-time 3 $AEGIS_HEALTH_URL  (retry x5)"
  plan ""
  plan "dry-run complete — no ssh executed."
  exit 0
fi

# --- live ssh ----------------------------------------------------------------

log "1/2  Rolling back on $AEGIS_HOST (git reset --hard HEAD~1 + restart)"
if ! ssh "$AEGIS_HOST" bash -s <<<"$REMOTE_BLOCK"; then
  err "on-box rollback failed — investigate journalctl -u aegis-web aegis-worker"
fi

log "2/2  Smoke-checking /healthz"
for attempt in 1 2 3 4 5; do
  if ssh "$AEGIS_HOST" "curl --fail --silent --max-time 3 $AEGIS_HEALTH_URL" >/dev/null; then
    log "rollback ok"
    exit 0
  fi
  sleep 2
done
err "healthz did not return 200 within 10s after rollback — investigate journalctl -u aegis-web"
