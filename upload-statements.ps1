# upload-statements.ps1
# Uploads all PDFs in C:\Users\fkozi\OneDrive\Radna površina\bank statements
# to AEGIS on Hetzner via SSH tunnel + bearer-authenticated /upload endpoint.
#
# Path: laptop -> SSH tunnel -> box localhost:5555 -> /upload -> arq worker
# No data via Cloudflare, no public exposure.
#
# Token loaded from env — never hardcode. Set in aegis.env or Windows credential store.
#   $env:AEGIS_UPLOAD_BEARER = "<API_BEARER_TOKEN from box's /etc/aegis/aegis.env>"

$ErrorActionPreference = "Stop"

$BoxIP      = "5.161.51.105"
$BoxUser    = "root"
$KeyFile    = "C:/Users/fkozi/.ssh/aegis_ed25519"
$BearerToken = $env:AEGIS_UPLOAD_BEARER
if (-not $BearerToken) {
    Write-Host "AEGIS_UPLOAD_BEARER not set. Export the API bearer before running." -ForegroundColor Red
    exit 1
}
$LocalPort  = 15555  # avoid conflict if 5555 already in use locally
$RemotePort = 5555
# Resolve folder via wildcard so the Croatian 'š' character doesn't matter
# regardless of how PowerShell decodes this script file.
$onedrive = $env:OneDrive
if (-not $onedrive) { $onedrive = "$env:USERPROFILE\OneDrive" }
$desktopDir = Get-ChildItem -Path $onedrive -Directory | Where-Object { $_.Name -like "Radna*" } | Select-Object -First 1
if (-not $desktopDir) { Write-Host "Cannot find OneDrive desktop dir" -ForegroundColor Red; exit 1 }
$PdfFolder = Join-Path $desktopDir.FullName "bank statements"
$ApiBase    = "http://127.0.0.1:$LocalPort"

# --- list PDFs ---
Write-Host "Scanning $PdfFolder..." -ForegroundColor Cyan
$pdfs = @(Get-ChildItem -Path $PdfFolder -Filter "*.pdf" -File)
Write-Host "Found $($pdfs.Count) PDFs to upload." -ForegroundColor Cyan
foreach ($p in $pdfs) {
    Write-Host "  - $($p.Name) ($([math]::Round($p.Length/1KB)) KB)"
}
Write-Host ""

# --- start SSH tunnel in background ---
Write-Host "Opening SSH tunnel to ${BoxUser}@${BoxIP} ($LocalPort -> ${RemotePort})..." -ForegroundColor Cyan
$tunnelArgs = @(
    "-i", $KeyFile,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-N",  # no remote command, just forward
    "-L", "${LocalPort}:127.0.0.1:${RemotePort}",
    "${BoxUser}@${BoxIP}"
)
$tunnel = Start-Process ssh -ArgumentList $tunnelArgs -NoNewWindow -PassThru
Start-Sleep -Seconds 3

if ($tunnel.HasExited) {
    Write-Host "SSH tunnel failed to start (exit code $($tunnel.ExitCode))." -ForegroundColor Red
    exit 1
}
Write-Host "Tunnel up (PID $($tunnel.Id))." -ForegroundColor Green
Write-Host ""

# --- sanity check via /healthz ---
try {
    $health = Invoke-RestMethod -Uri "$ApiBase/healthz" -TimeoutSec 5
    if ($health.ok) {
        Write-Host "/healthz reachable through tunnel: ok" -ForegroundColor Green
    } else {
        Write-Host "/healthz returned unexpected: $($health | ConvertTo-Json)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "Cannot reach /healthz through tunnel: $($_.Exception.Message)" -ForegroundColor Red
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host ""

# --- upload each PDF ---
$Headers = @{ Authorization = "Bearer $BearerToken" }
$results = @()

foreach ($p in $pdfs) {
    Write-Host "==> uploading $($p.Name)" -ForegroundColor Cyan
    # curl.exe handles multipart cleanly even on PS 5.1 (Invoke-RestMethod -Form is PS7+).
    # Plain HTTP to localhost - no SSL/cert issues.
    $raw = & curl.exe -s -S `
        -H "Authorization: Bearer $BearerToken" `
        -F "file=@$($p.FullName)" `
        --max-time 60 `
        "$ApiBase/upload" 2>&1
    try {
        $r = $raw | ConvertFrom-Json
        if ($r.document_id) {
            $status = if ($r.duplicate_of_existing) { "DUPLICATE" } else { "QUEUED" }
            Write-Host "    document_id=$($r.document_id)  parse_status=$($r.parse_status)  $status" -ForegroundColor Green
            $results += [pscustomobject]@{
                file        = $p.Name
                document_id = $r.document_id
                parse_status = $r.parse_status
                duplicate   = $r.duplicate_of_existing
            }
        } else {
            Write-Host "    UPLOAD FAILED: $raw" -ForegroundColor Red
            $results += [pscustomobject]@{
                file = $p.Name; document_id = $null; parse_status = "FAILED"; duplicate = $false
            }
        }
    } catch {
        Write-Host "    UPLOAD FAILED (non-JSON response): $raw" -ForegroundColor Red
        $results += [pscustomobject]@{
            file = $p.Name; document_id = $null; parse_status = "FAILED"; duplicate = $false
        }
    }
}

# --- close tunnel ---
Write-Host ""
Write-Host "Closing SSH tunnel..." -ForegroundColor Cyan
Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue

# --- summary ---
Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Yellow
$results | Format-Table -AutoSize
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Worker is processing in background. Each PDF takes ~30-90 sec to parse."
Write-Host "  2. Watch worker progress on the box:"
Write-Host "       ssh -i $KeyFile ${BoxUser}@${BoxIP} 'journalctl -u aegis-worker -f'"
Write-Host "  3. Re-run this script anytime - duplicates are deduped by hash, no re-parse cost."
Write-Host "  4. DO NOT visit https://aegis.commerafunding.com/ui/ until Cloudflare Access is locked."
Write-Host "     The processed data lives in Supabase (encrypted) and the dashboard exposes it."
