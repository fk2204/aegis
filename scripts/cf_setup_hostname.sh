#!/usr/bin/env bash
# scripts/cf_setup_hostname.sh — one-shot Cloudflare setup for AEGIS.
#
# Reads CF_TOKEN from env. Does NOT log the token.
#
# Steps:
#   1. Verify token.
#   2. Find the Cloudflare account + the commerafunding.com zone.
#   3. Find the AEGIS tunnel.
#   4. Add public hostname `aegis.commerafunding.com` to the tunnel
#      with service `http://localhost:5555`.
#   5. Cloudflare auto-creates the DNS CNAME (we verify it lands).
#   6. Create the Access Application gating that hostname.
#   7. Create an Allow policy with the operator's email
#      (fkozina92@gmail.com). Pass additional emails as args to add
#      more workers in the same step.
#
# Usage:
#   $env:CF_TOKEN='cfut_xxx'; bash scripts/cf_setup_hostname.sh
#   $env:CF_TOKEN='cfut_xxx'; bash scripts/cf_setup_hostname.sh worker1@x.com worker2@y.com
#
# Idempotent-ish: re-running should re-print existing IDs without
# creating duplicates (Cloudflare returns 409 on conflicting names; we
# fall back to looking up the existing object).

set -euo pipefail

if [ -z "${CF_TOKEN:-}" ]; then
  echo "ERROR: CF_TOKEN env var is required (the Cloudflare API token)." >&2
  exit 1
fi

API=https://api.cloudflare.com/client/v4
H_AUTH="Authorization: Bearer $CF_TOKEN"
H_JSON="Content-Type: application/json"

# Tunnel ID lives in the systemd unit on the box; matches what's running.
TUNNEL_ID="3182a964-d699-40cf-a763-030d7dacb026"
TARGET_SUBDOMAIN="aegis"
TARGET_ZONE="commerafunding.com"
TARGET_HOSTNAME="${TARGET_SUBDOMAIN}.${TARGET_ZONE}"
TARGET_SERVICE="http://localhost:5555"
APP_NAME="AEGIS Dashboard"
OPERATOR_EMAIL="fkozina92@gmail.com"

# Additional worker emails from CLI args
EXTRA_EMAILS=("$@")

log()  { printf "\033[1;34m[cf]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[cf]\033[0m %s\n" "$*" >&2; exit 1; }

call() {
  local method=$1 path=$2
  shift 2
  # --ssl-no-revoke needed on Windows schannel where the OCSP check fails;
  # cert chain is still validated. Linux/Mac just ignore the flag.
  curl -sS --ssl-no-revoke -X "$method" -H "$H_AUTH" -H "$H_JSON" "$@" "$API$path"
}

jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print(${1})"; }

# ---------- Step 1: verify token ----------
log "1/7 verify token"
VERIFY=$(call GET /user/tokens/verify)
OK=$(echo "$VERIFY" | jget "d.get('success')")
[ "$OK" = "True" ] || fail "token verify failed: $(echo "$VERIFY" | jget "d.get('errors')")"
log "    token OK (status=$(echo "$VERIFY" | jget "d['result']['status']"))"

# ---------- Step 2 + 3: zone + account derived from zone ----------
log "2/7 find zone $TARGET_ZONE (also gives us the account ID)"
ZONES=$(call GET "/zones?name=$TARGET_ZONE")
ZONE_ID=$(echo "$ZONES" | jget "d['result'][0]['id'] if d['result'] else ''")
[ -n "$ZONE_ID" ] || fail "zone $TARGET_ZONE not in this Cloudflare account (got: $(echo "$ZONES" | head -c 300))"
ACCOUNT_ID=$(echo "$ZONES" | jget "d['result'][0]['account']['id']")
ACCOUNT_NAME=$(echo "$ZONES" | jget "d['result'][0]['account']['name']")
log "    zone_id=$ZONE_ID"
log "    account: $ACCOUNT_NAME ($ACCOUNT_ID)"

log "3/7 (skipped — account derived above)"

# ---------- Step 4: confirm tunnel exists ----------
log "4/7 confirm tunnel $TUNNEL_ID"
TUNNEL=$(call GET "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID")
TUNNEL_NAME=$(echo "$TUNNEL" | jget "d.get('result',{}).get('name','?')")
log "    tunnel name=$TUNNEL_NAME"

# ---------- Step 5: add public hostname to tunnel ----------
log "5/7 add public hostname $TARGET_HOSTNAME -> $TARGET_SERVICE"
CONFIG=$(call GET "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations")
EXISTING_INGRESS=$(echo "$CONFIG" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ing = (d.get('result') or {}).get('config', {}).get('ingress', [])
print(json.dumps(ing))
")
log "    current ingress rules: $EXISTING_INGRESS"

NEW_CONFIG=$(python3 <<EOF
import json
existing = json.loads('''$EXISTING_INGRESS''')
new_rule = {
    "hostname": "$TARGET_HOSTNAME",
    "service": "$TARGET_SERVICE",
    "originRequest": {}
}
# Strip any prior rule for our hostname; keep the catch-all at the end.
filtered = [r for r in existing if r.get("hostname") != "$TARGET_HOSTNAME"]
catchall = [r for r in filtered if not r.get("hostname")]
keep = [r for r in filtered if r.get("hostname")]
ingress = keep + [new_rule]
if catchall:
    ingress += catchall
else:
    ingress.append({"service": "http_status:404"})
print(json.dumps({"config": {"ingress": ingress}}))
EOF
)

PUT_RESULT=$(call PUT "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" --data "$NEW_CONFIG")
OK=$(echo "$PUT_RESULT" | jget "d.get('success')")
[ "$OK" = "True" ] || fail "tunnel config update failed: $(echo "$PUT_RESULT" | jget "d.get('errors')")"
log "    ingress updated"

# ---------- Step 6: DNS CNAME ----------
log "6/7 ensure DNS CNAME $TARGET_HOSTNAME -> $TUNNEL_ID.cfargotunnel.com"
EXISTING_DNS=$(call GET "/zones/$ZONE_ID/dns_records?name=$TARGET_HOSTNAME&type=CNAME")
DNS_ID=$(echo "$EXISTING_DNS" | jget "d['result'][0]['id'] if d['result'] else ''")
DNS_PAYLOAD=$(cat <<EOF
{"type":"CNAME","name":"$TARGET_SUBDOMAIN","content":"$TUNNEL_ID.cfargotunnel.com","proxied":true,"ttl":1}
EOF
)
if [ -z "$DNS_ID" ]; then
  CREATE_DNS=$(call POST "/zones/$ZONE_ID/dns_records" --data "$DNS_PAYLOAD")
  OK=$(echo "$CREATE_DNS" | jget "d.get('success')")
  [ "$OK" = "True" ] || fail "DNS create failed: $(echo "$CREATE_DNS" | jget "d.get('errors')")"
  log "    DNS CNAME created"
else
  log "    DNS CNAME already exists (id=$DNS_ID); skipping create"
fi

# ---------- Step 7: Access app + Allow policy ----------
log "7/7 Access application + policy"

# Find existing Access app for this hostname
APPS=$(call GET "/accounts/$ACCOUNT_ID/access/apps?domain=$TARGET_HOSTNAME")
APP_ID=$(echo "$APPS" | jget "d['result'][0]['id'] if d['result'] else ''")

if [ -z "$APP_ID" ]; then
  APP_PAYLOAD=$(cat <<EOF
{
  "name": "$APP_NAME",
  "domain": "$TARGET_HOSTNAME",
  "type": "self_hosted",
  "session_duration": "24h",
  "auto_redirect_to_identity": false,
  "allowed_idps": []
}
EOF
)
  CREATE_APP=$(call POST "/accounts/$ACCOUNT_ID/access/apps" --data "$APP_PAYLOAD")
  OK=$(echo "$CREATE_APP" | jget "d.get('success')")
  [ "$OK" = "True" ] || fail "Access app create failed: $(echo "$CREATE_APP" | jget "d.get('errors')")"
  APP_ID=$(echo "$CREATE_APP" | jget "d['result']['id']")
  log "    Access app created (id=$APP_ID)"
else
  log "    Access app already exists (id=$APP_ID)"
fi

# Build policy include list (operator + any extra emails)
EMAILS=("$OPERATOR_EMAIL" "${EXTRA_EMAILS[@]}")
INCLUDE_JSON=$(python3 <<EOF
import json
emails = json.loads('''$(printf '"%s",' "${EMAILS[@]}" | sed 's/,$//' | xargs -I{} echo "[{}]")''')
print(json.dumps([{"email": {"email": e}} for e in emails]))
EOF
)
log "    policy will allow: $(printf '%s ' "${EMAILS[@]}")"

POLICY_PAYLOAD=$(cat <<EOF
{
  "name": "Allow operators and workers",
  "decision": "allow",
  "include": $INCLUDE_JSON
}
EOF
)
EXISTING_POLICIES=$(call GET "/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies")
POLICY_ID=$(echo "$EXISTING_POLICIES" | jget "d['result'][0]['id'] if d['result'] else ''")
if [ -z "$POLICY_ID" ]; then
  CREATE_POLICY=$(call POST "/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies" --data "$POLICY_PAYLOAD")
  OK=$(echo "$CREATE_POLICY" | jget "d.get('success')")
  [ "$OK" = "True" ] || fail "policy create failed: $(echo "$CREATE_POLICY" | jget "d.get('errors')")"
  log "    Allow policy created"
else
  UPDATE_POLICY=$(call PUT "/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies/$POLICY_ID" --data "$POLICY_PAYLOAD")
  OK=$(echo "$UPDATE_POLICY" | jget "d.get('success')")
  [ "$OK" = "True" ] || fail "policy update failed: $(echo "$UPDATE_POLICY" | jget "d.get('errors')")"
  log "    Allow policy updated"
fi

echo ""
log "DONE."
log ""
log "  Worker URL:   https://$TARGET_HOSTNAME"
log "  Account ID:   $ACCOUNT_ID"
log "  Zone ID:      $ZONE_ID"
log "  Tunnel ID:    $TUNNEL_ID"
log "  Access app:   $APP_ID"
log ""
log "Allowed emails:"
for e in "${EMAILS[@]}"; do log "  - $e"; done
log ""
log "Test it: open https://$TARGET_HOSTNAME in a private browser window."
log "         You'll get a Cloudflare email-PIN prompt, then the AEGIS dashboard."
