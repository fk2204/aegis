#!/usr/bin/env bash
# scripts/cf_provision_tunnel.sh — full Cloudflare provisioning for AEGIS.
#
# Creates a NEW tunnel in the same Cloudflare account as commerafunding.com,
# maps aegis.commerafunding.com to it, creates the Access app + policy,
# and emits the new tunnel token so the operator can swap cloudflared on
# the box. Idempotent: re-running reuses existing objects.
#
# Reads CF_TOKEN from env. Does NOT log the token.

set -euo pipefail

[ -z "${CF_TOKEN:-}" ] && { echo "ERROR: CF_TOKEN env var required" >&2; exit 1; }

API=https://api.cloudflare.com/client/v4
TARGET_ZONE="commerafunding.com"
TARGET_SUBDOMAIN="aegis"
TARGET_HOSTNAME="${TARGET_SUBDOMAIN}.${TARGET_ZONE}"
TARGET_SERVICE="http://localhost:5555"
TUNNEL_NAME="aegis-prod"
APP_NAME="AEGIS Dashboard"
OPERATOR_EMAIL="fkozina92@gmail.com"
EXTRA_EMAILS=("$@")

log()  { printf "\033[1;34m[cf]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[cf]\033[0m %s\n" "$*" >&2; exit 1; }
call() {
  local method=$1 path=$2
  shift 2
  curl -sS --ssl-no-revoke -X "$method" \
    -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
    "$@" "$API$path"
}
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print(${1})"; }

# Step 1: token + zone (also yields account)
log "1/8 verify token + find zone"
V=$(call GET /user/tokens/verify)
[ "$(echo "$V" | jget "d.get('success')")" = "True" ] || fail "verify: $V"

Z=$(call GET "/zones?name=$TARGET_ZONE")
ZONE_ID=$(echo "$Z" | jget "d['result'][0]['id'] if d['result'] else ''")
[ -n "$ZONE_ID" ] || fail "zone $TARGET_ZONE not accessible: $Z"
ACCOUNT_ID=$(echo "$Z" | jget "d['result'][0]['account']['id']")
ACCOUNT_NAME=$(echo "$Z" | jget "d['result'][0]['account']['name']")
log "    zone=$ZONE_ID  account=$ACCOUNT_NAME ($ACCOUNT_ID)"

# Step 2: find or create tunnel "aegis-prod"
log "2/8 find or create tunnel '$TUNNEL_NAME'"
TUNNELS=$(call GET "/accounts/$ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false")
TUNNEL_ID=$(echo "$TUNNELS" | jget "d['result'][0]['id'] if d['result'] else ''")
TUNNEL_TOKEN=""

if [ -z "$TUNNEL_ID" ]; then
  # Tunnel needs a TunnelSecret (32 random bytes, base64). Generate one.
  SECRET=$(python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")
  CREATE=$(call POST "/accounts/$ACCOUNT_ID/cfd_tunnel" --data "{\"name\":\"$TUNNEL_NAME\",\"tunnel_secret\":\"$SECRET\",\"config_src\":\"cloudflare\"}")
  [ "$(echo "$CREATE" | jget "d.get('success')")" = "True" ] || fail "create tunnel: $CREATE"
  TUNNEL_ID=$(echo "$CREATE" | jget "d['result']['id']")
  log "    created tunnel $TUNNEL_ID"
else
  log "    reusing existing tunnel $TUNNEL_ID"
fi

# Fetch the connector token (separate endpoint)
TOKEN_RESP=$(call GET "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/token")
TUNNEL_TOKEN=$(echo "$TOKEN_RESP" | jget "d['result']")
[ -n "$TUNNEL_TOKEN" ] && [ "$TUNNEL_TOKEN" != "None" ] || fail "get connector token: $TOKEN_RESP"
log "    connector token retrieved (length ${#TUNNEL_TOKEN})"

# Step 3: set tunnel ingress (Cloudflare-managed config)
log "3/8 set ingress $TARGET_HOSTNAME -> $TARGET_SERVICE"
INGRESS_CONFIG=$(python3 <<EOF
import json
print(json.dumps({
  "config": {
    "ingress": [
      {"hostname": "$TARGET_HOSTNAME", "service": "$TARGET_SERVICE", "originRequest": {}},
      {"service": "http_status:404"}
    ]
  }
}))
EOF
)
PUT_CFG=$(call PUT "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" --data "$INGRESS_CONFIG")
[ "$(echo "$PUT_CFG" | jget "d.get('success')")" = "True" ] || fail "set ingress: $PUT_CFG"
log "    ingress set"

# Step 4: DNS CNAME -> <tunnel-id>.cfargotunnel.com
log "4/8 DNS CNAME $TARGET_HOSTNAME"
EX_DNS=$(call GET "/zones/$ZONE_ID/dns_records?name=$TARGET_HOSTNAME&type=CNAME")
DNS_ID=$(echo "$EX_DNS" | jget "d['result'][0]['id'] if d['result'] else ''")
DNS_PAYLOAD="{\"type\":\"CNAME\",\"name\":\"$TARGET_SUBDOMAIN\",\"content\":\"$TUNNEL_ID.cfargotunnel.com\",\"proxied\":true,\"ttl\":1}"
if [ -z "$DNS_ID" ]; then
  CR=$(call POST "/zones/$ZONE_ID/dns_records" --data "$DNS_PAYLOAD")
  [ "$(echo "$CR" | jget "d.get('success')")" = "True" ] || fail "DNS create: $CR"
  log "    DNS created"
else
  CR=$(call PUT "/zones/$ZONE_ID/dns_records/$DNS_ID" --data "$DNS_PAYLOAD")
  [ "$(echo "$CR" | jget "d.get('success')")" = "True" ] || fail "DNS update: $CR"
  log "    DNS updated"
fi

# Step 5: Access application
log "5/8 Access application"
APPS=$(call GET "/accounts/$ACCOUNT_ID/access/apps?domain=$TARGET_HOSTNAME")
APP_ID=$(echo "$APPS" | jget "d['result'][0]['id'] if d['result'] else ''")
APP_PAYLOAD="{\"name\":\"$APP_NAME\",\"domain\":\"$TARGET_HOSTNAME\",\"type\":\"self_hosted\",\"session_duration\":\"24h\"}"
if [ -z "$APP_ID" ]; then
  AR=$(call POST "/accounts/$ACCOUNT_ID/access/apps" --data "$APP_PAYLOAD")
  [ "$(echo "$AR" | jget "d.get('success')")" = "True" ] || fail "Access app create: $AR"
  APP_ID=$(echo "$AR" | jget "d['result']['id']")
  log "    Access app created $APP_ID"
else
  log "    Access app exists $APP_ID"
fi

# Step 6: Access policy
log "6/8 Access policy"
EMAILS=("$OPERATOR_EMAIL" "${EXTRA_EMAILS[@]}")
INCLUDE=$(AEGIS_EMAILS="${EMAILS[*]}" python3 -c "
import os, json
emails = os.environ['AEGIS_EMAILS'].split()
print(json.dumps([{'email': {'email': e}} for e in emails]))
")
POLICY_PAYLOAD="{\"name\":\"Allow operators and workers\",\"decision\":\"allow\",\"include\":$INCLUDE}"
EX_POL=$(call GET "/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies")
POLICY_ID=$(echo "$EX_POL" | jget "d['result'][0]['id'] if d['result'] else ''")
if [ -z "$POLICY_ID" ]; then
  PR=$(call POST "/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies" --data "$POLICY_PAYLOAD")
  [ "$(echo "$PR" | jget "d.get('success')")" = "True" ] || fail "policy create: $PR"
else
  PR=$(call PUT "/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies/$POLICY_ID" --data "$POLICY_PAYLOAD")
  [ "$(echo "$PR" | jget "d.get('success')")" = "True" ] || fail "policy update: $PR"
fi
log "    policy allows: $(printf '%s ' "${EMAILS[@]}")"

# Step 7: emit new tunnel token (operator pastes into box systemd unit)
log "7/8 capturing connector token for box swap"
SECRETS_FILE="$(dirname "$0")/../.tmp/aegis_tunnel_token.txt"
mkdir -p "$(dirname "$SECRETS_FILE")"
echo "$TUNNEL_TOKEN" > "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"
log "    saved to: $SECRETS_FILE  (gitignored)"

# Step 8: summary
log "8/8 DONE"
echo ""
log "  URL:         https://$TARGET_HOSTNAME"
log "  Tunnel ID:   $TUNNEL_ID"
log "  Access app:  $APP_ID"
log "  Allowed:     $(printf '%s ' "${EMAILS[@]}")"
log ""
log "  Next step: swap the cloudflared on the box to use this new tunnel."
log "  The new tunnel token is at:"
log "    $SECRETS_FILE"
log ""
log "  Or run:  bash scripts/cf_swap_box_tunnel.sh"
