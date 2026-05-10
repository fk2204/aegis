#!/usr/bin/env bash
# install.sh — idempotent first-time setup on a fresh Hetzner CPX21 (Ubuntu 24 LTS).
#
# What this does:
#   1. Creates the unprivileged ``aegis`` user that systemd runs the
#      web + worker processes as.
#   2. Installs python3.12, uv, redis-server, cloudflared, and the
#      WeasyPrint native runtime (Pango/Cairo/HarfBuzz) — without those
#      libs the Tier-1 disclosure renders fail at runtime.
#   3. Lays out /opt/aegis (code), /etc/aegis (env), /var/log/aegis (logs).
#   4. Installs the systemd units + logrotate config.
#
# Idempotent: re-running is safe. Reinstalls dependencies that are
# missing, no-ops otherwise.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "install.sh must run as root (sudo bash install.sh)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> 1/8  Updating apt index"
apt-get update -y

echo "==> 2/8  Installing system packages"
# Python toolchain + native build deps for any wheels that need them
# (pikepdf, weasyprint dependencies). build-essential pulls gcc/make.
# rsync + git: needed by scripts/deploy.sh on the remote (subsequent
# pulls + sync). curl + ca-certificates: needed by the uv installer
# below and any HTTPS fetch (TLS chain). WeasyPrint needs Pango /
# Cairo / HarfBuzz native libs — without them Tier 1 disclosure
# renders fail at runtime.
apt-get install -y \
  python3.12 python3.12-venv python3.12-dev python3-pip \
  build-essential \
  redis-server \
  rsync git \
  curl ca-certificates \
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  libcairo2 libgdk-pixbuf-2.0-0 fonts-liberation \
  logrotate

echo "==> 3/8  Installing uv (if missing)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  install -m 0755 "$HOME/.local/bin/uv" /usr/local/bin/uv
fi

echo "==> 4/8  Installing cloudflared (if missing)"
if ! command -v cloudflared >/dev/null 2>&1; then
  curl -L --output /usr/local/bin/cloudflared \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x /usr/local/bin/cloudflared
fi

echo "==> 5/8  Creating aegis user + dirs"
if ! id aegis >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/aegis --shell /usr/sbin/nologin aegis
fi
# /home/aegis explicit: useradd --create-home only fires on first run.
# If a prior install left the user without a home dir, or some external
# tool created /home/aegis with wrong ownership, this fixes it. Required
# because the install-time `uv sync` below runs as the aegis user
# OUTSIDE systemd (no ProtectHome here) and uv defaults its cache to
# ~/.cache/uv until UV_CACHE_DIR overrides — without an aegis-owned
# /home/aegis the cache init crashes with "Permission denied".
install -d -o aegis -g aegis -m 0755 /home/aegis
install -d -o aegis -g aegis -m 0755 /opt/aegis
install -d -o root  -g root  -m 0755 /etc/aegis
install -d -o aegis -g aegis -m 0750 /var/log/aegis
# /var/lib/aegis tree: uploads/ holds incoming PDFs (worker deletes
# them in a finally block — never long-lived); uv-cache/ is the
# managed UV_CACHE_DIR target referenced from /etc/aegis/aegis.env so
# runtime uv invocations under systemd (which has ProtectHome=true)
# can still write cache.
install -d -o aegis -g aegis -m 0750 /var/lib/aegis
install -d -o aegis -g aegis -m 0750 /var/lib/aegis/uploads
install -d -o aegis -g aegis -m 0750 /var/lib/aegis/uv-cache

if [[ ! -f /etc/aegis/aegis.env ]]; then
  cat <<'EOF' >/etc/aegis/aegis.env
# /etc/aegis/aegis.env — populated by the operator. NEVER commit.
# Required:
AEGIS_DATA_RESIDENCY_CONFIRMED=true
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
AWS_REGION=us-east-1
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
API_BEARER_TOKEN=
ZOHO_CLIENT_ID=
ZOHO_CLIENT_SECRET=
ZOHO_REFRESH_TOKEN=
ZOHO_WEBHOOK_SECRET=
REDIS_URL=redis://127.0.0.1:6379
AEGIS_STORAGE_BACKEND=supabase
AEGIS_UPLOAD_DIR=/var/lib/aegis/uploads
# uv cache — must be a path the systemd-managed process can write to
# (see ReadWritePaths in aegis-{web,worker}.service). Default /home is
# blocked by ProtectHome=true.
UV_CACHE_DIR=/var/lib/aegis/uv-cache
EOF
  chmod 0640 /etc/aegis/aegis.env
  chown root:aegis /etc/aegis/aegis.env
  echo "    wrote skeleton /etc/aegis/aegis.env — POPULATE BEFORE STARTING SERVICES"
fi

echo "==> 6/8  Syncing repo into /opt/aegis"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.git' \
  "$REPO_ROOT/" /opt/aegis/
chown -R aegis:aegis /opt/aegis

echo "==> 7/8  Installing Python deps via uv"
sudo -u aegis -H bash -lc 'cd /opt/aegis && uv sync'

echo "==> 8/8  Installing systemd units + logrotate"
install -m 0644 "$REPO_ROOT/deploy/aegis-web.service"     /etc/systemd/system/aegis-web.service
install -m 0644 "$REPO_ROOT/deploy/aegis-worker.service"  /etc/systemd/system/aegis-worker.service
install -m 0644 "$REPO_ROOT/deploy/logrotate.aegis"       /etc/logrotate.d/aegis

systemctl daemon-reload

# Enable all three units so they come up on reboot, but only START
# redis-server here. aegis-web and aegis-worker would fail their
# data-residency boot guard until the operator populates
# /etc/aegis/aegis.env (AEGIS_DATA_RESIDENCY_CONFIRMED, AWS creds,
# SUPABASE_*, API_BEARER_TOKEN). They start in the operator-driven
# step below.
systemctl enable redis-server aegis-web aegis-worker
systemctl start redis-server

echo
echo "Install complete. Next steps:"
echo "  1) populate /etc/aegis/aegis.env from /opt/aegis/deploy/aegis.env.example"
echo "  2) systemctl start aegis-web aegis-worker"
echo "  3) systemctl status aegis-web --no-pager"
echo "  4) curl http://127.0.0.1:5555/healthz"
echo
echo "  Cloudflare Tunnel (separate, before the box is reachable externally):"
echo "    see /opt/aegis/deploy/cloudflared-config.yml.example"
