# scripts/deploy.ps1 — workstation one-button deploy + audit + restart.
#
# Runs the full canonical Hetzner deploy in one SSH session: pull,
# uv sync, apply pending migrations, dump funders table state, restart
# systemd units. Saves you typing the multi-line bash command every
# time.
#
# Usage (from C:\Users\fkozi\aegis or anywhere):
#   .\scripts\deploy.ps1
#
# Add to PATH or alias for one-keystroke deploys.
#
# No arguments needed. The SSH hostname is the prod box.

$ErrorActionPreference = 'Stop'

$RemoteScript = @'
# -e (exit on error) + pipefail; intentionally NO -u — /etc/aegis/aegis.env
# contains $-bearing values (e.g. DB password) that bash would try to expand
# as variable refs under nounset. The `set -a; source; set +a` block must
# allow unset-variable references during source.
set -eo pipefail
cd /opt/aegis
echo "==> git pull"
git pull --ff-only
echo
echo "==> uv sync"
uv sync
echo
echo "==> apply migrations"
set -a; source /etc/aegis/aegis.env; set +a
.venv/bin/python scripts/apply_migrations.py --target prod
echo
echo "==> funders table audit"
.venv/bin/python scripts/audit_funders_table.py
echo
echo "==> restart aegis-web + aegis-worker"
sudo systemctl restart aegis-web aegis-worker
sleep 2
echo
echo "==> service status"
sudo systemctl is-active aegis-web aegis-worker
echo
echo "==> deploy ok"
'@

Write-Host "Connecting to aegis@aegis-ssh.commerafunding.com..." -ForegroundColor Cyan
ssh aegis@aegis-ssh.commerafunding.com $RemoteScript
