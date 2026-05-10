# Canary upload — pushes the smallest PDF in OneDrive\Desktop\bank statements
# through the production /upload endpoint, polls parse_status until done,
# tails worker logs. Single file = single test of "do Bedrock creds work?"
# before we send all 11 statements.

$ErrorActionPreference = "Stop"

$BoxIP       = "5.161.51.105"
$BoxUser     = "root"
$KeyFile     = "C:/Users/fkozi/.ssh/aegis_ed25519"
$LocalPort   = 15555
$RemotePort  = 5555

# Pull bearer token off the box (never echo in full)
$BearerToken = & ssh -i $KeyFile -o BatchMode=yes "$BoxUser@$BoxIP" "grep -E '^API_BEARER_TOKEN=' /etc/aegis/aegis.env | cut -d= -f2-"
if (-not $BearerToken) { throw "bearer missing on box" }
Write-Host ("  bearer: {0}... ({1} chars)" -f $BearerToken.Substring(0,8), $BearerToken.Length)

# Find canary PDF
$onedrive = $env:OneDrive
if (-not $onedrive) { $onedrive = "$env:USERPROFILE\OneDrive" }
$desktopDir = Get-ChildItem -Path $onedrive -Directory | Where-Object { $_.Name -like "Radna*" } | Select-Object -First 1
$PdfFolder = Join-Path $desktopDir.FullName "bank statements"
$canary = (Get-ChildItem -Path $PdfFolder -Filter "*.pdf" -File | Sort-Object Length)[0]
Write-Host ("  canary: {0} ({1} KB)" -f $canary.Name, [Math]::Round($canary.Length/1KB))

# Tunnel
$tunnelArgs = @(
  "-i", $KeyFile,
  "-o", "BatchMode=yes",
  "-o", "StrictHostKeyChecking=no",
  "-o", "ExitOnForwardFailure=yes",
  "-o", "ServerAliveInterval=30",
  "-N",
  "-L", "$($LocalPort):127.0.0.1:$($RemotePort)",
  "$($BoxUser)@$($BoxIP)"
)
$tunnel = Start-Process ssh -ArgumentList $tunnelArgs -NoNewWindow -PassThru
Start-Sleep -Seconds 3
if ($tunnel.HasExited) { throw "tunnel failed (exit $($tunnel.ExitCode))" }
Write-Host ("  tunnel up (PID {0})" -f $tunnel.Id)

try {
  Write-Host ""
  Write-Host "=== POST /upload ==="
  $raw = & curl.exe -s -S -H "Authorization: Bearer $BearerToken" -F "file=@$($canary.FullName)" --max-time 60 "http://127.0.0.1:$LocalPort/upload"
  Write-Host "  $raw"
  $resp = $raw | ConvertFrom-Json
  $docId = $resp.document_id
  if (-not $docId) { throw "no document_id returned" }
  Write-Host "  document_id: $docId"

  Write-Host ""
  Write-Host "=== poll parse_status (server-side SQL via uv-run) ==="
  $pollScript = "sudo -u aegis bash -lc 'cd /opt/aegis && /usr/local/bin/uv run python scripts/_status_probe.py $docId'"
  for ($i = 1; $i -le 18; $i++) {
    Start-Sleep -Seconds 5
    $status = & ssh -i $KeyFile -o BatchMode=yes "$($BoxUser)@$($BoxIP)" $pollScript 2>&1
    Write-Host ("  poll {0,2} ({1}): {2}" -f $i, (Get-Date -Format HH:mm:ss), ($status -join " "))
    if ($status -match "proceed|review|manual_review|error") { break }
  }

  Write-Host ""
  Write-Host "=== last 25 worker log lines ==="
  & ssh -i $KeyFile -o BatchMode=yes "$($BoxUser)@$($BoxIP)" "journalctl -u aegis-worker -n 25 --no-pager"
}
finally {
  Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
  Write-Host ""
  Write-Host "  tunnel closed"
}
