# install-cloudflare-tunnel.ps1
# Installs Cloudflare Tunnel as a systemd service on the Hetzner box.
#
# Token loaded from env — never hardcode. Set in aegis.env or Windows credential store.
#   $env:AEGIS_TUNNEL_TOKEN = "<cloudflare tunnel JWT>"

$ErrorActionPreference = "Stop"

$BoxIP   = "5.161.51.105"
$BoxUser = "root"
$KeyFile = "C:/Users/fkozi/.ssh/aegis_ed25519"
$Token   = $env:AEGIS_TUNNEL_TOKEN
if (-not $Token) {
    Write-Host "AEGIS_TUNNEL_TOKEN not set. Export the Cloudflare tunnel JWT before running." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== Step A: installing cloudflared as systemd service on $BoxIP ===" -ForegroundColor Cyan
Write-Host ""

$installCmd = "cloudflared service install $Token"
ssh -i $KeyFile -o BatchMode=yes -o StrictHostKeyChecking=no "$BoxUser@$BoxIP" $installCmd

Write-Host ""
Write-Host "=== Step B: systemd status ===" -ForegroundColor Cyan
Write-Host ""

ssh -i $KeyFile -o BatchMode=yes "$BoxUser@$BoxIP" "systemctl status cloudflared --no-pager | head -20"

Write-Host ""
Write-Host "=== Step C: recent logs ===" -ForegroundColor Cyan
Write-Host ""

ssh -i $KeyFile -o BatchMode=yes "$BoxUser@$BoxIP" "journalctl -u cloudflared -n 15 --no-pager"

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host ""
Write-Host "If you see 'Active: active (running)' and 'Connection ... registered', the tunnel is up." -ForegroundColor Yellow
Write-Host "Refresh the Cloudflare dashboard tab. Connection Status should turn green." -ForegroundColor Yellow
Write-Host ""
