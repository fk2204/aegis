#!/usr/bin/env bash
# scripts/cf_swap_box_tunnel.sh — point cloudflared on the box at the new tunnel.
#
# Reads the new tunnel token from .tmp/aegis_tunnel_token.txt (created
# by scripts/cf_provision_tunnel.sh) and:
#   1. SCPs a new systemd drop-in to the box that overrides ExecStart
#      with the new token.
#   2. Restarts cloudflared.
#   3. Verifies the tunnel reports healthy + the public URL responds.

set -euo pipefail

TOKEN_FILE="$(dirname "$0")/../.tmp/aegis_tunnel_token.txt"
[ -f "$TOKEN_FILE" ] || { echo "ERROR: token file missing. Run scripts/cf_provision_tunnel.sh first." >&2; exit 1; }
TUNNEL_TOKEN=$(< "$TOKEN_FILE")
[ -n "$TUNNEL_TOKEN" ] || { echo "ERROR: token file is empty" >&2; exit 1; }

KEY="C:/Users/fkozi/.ssh/aegis_ed25519"
HOST="root@5.161.51.105"
TARGET_HOSTNAME="aegis.commerafunding.com"

log()  { printf "\033[1;34m[swap]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[swap]\033[0m %s\n" "$*" >&2; exit 1; }

log "1/4 backing up current cloudflared.service"
ssh -i "$KEY" -o BatchMode=yes "$HOST" \
  "cp /etc/systemd/system/cloudflared.service /etc/systemd/system/cloudflared.service.bak.\$(date +%s)"

log "2/4 writing new ExecStart with the new tunnel token"
ssh -i "$KEY" -o BatchMode=yes "$HOST" "bash -c 'cat > /etc/systemd/system/cloudflared.service' <<UNIT
[Unit]
Description=cloudflared
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=15
Type=notify
ExecStart=/usr/local/bin/cloudflared --no-autoupdate tunnel run --token $TUNNEL_TOKEN
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
UNIT
"

log "3/4 reload systemd + restart cloudflared"
ssh -i "$KEY" -o BatchMode=yes "$HOST" \
  "systemctl daemon-reload && systemctl restart cloudflared && sleep 4 && systemctl is-active cloudflared"

log "4/4 wait for tunnel to come up + probe public URL"
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 4
  CODE=$(curl -sS --ssl-no-revoke -o /dev/null -w '%{http_code}' --max-time 8 "https://$TARGET_HOSTNAME/healthz" 2>&1 || echo "000")
  echo "    probe $i: https://$TARGET_HOSTNAME/healthz -> $CODE"
  case "$CODE" in
    200|302) log "    URL responding!"; break ;;
  esac
done

log ""
log "Open in browser: https://$TARGET_HOSTNAME"
log "You'll get a Cloudflare email-PIN gate, then the AEGIS dashboard."
