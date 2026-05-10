# scripts/reparse_all.ps1 — destructive: wipe all parsed-document state
# and re-upload every PDF in the OneDrive bank-statements folder.
#
# When to use:
#   - After a parser fix lands (e.g. EOF threshold bump 2026-05-10) and
#     you want existing manual_review/pending docs re-processed.
#   - One-shot. Don't run on every code change; only when the parser's
#     decision logic actually changed.
#
# What it does (in order):
#   1. Lists all `documents` rows on the box's Supabase via the bearer API.
#   2. Prints a confirmation prompt with the count + names.
#   3. After explicit Y/n: deletes every documents/transactions/analyses
#      row (cascade order matters — transactions first, then analyses,
#      then documents).
#   4. Re-uploads every PDF in the bank-statements folder via the existing
#      SSH-tunnel + /upload flow (the same one upload-statements.ps1 uses).
#   5. Polls parse_status for ~60s per doc and prints final state.
#
# What it does NOT do:
#   - Delete merchants (operator-curated records — keep them).
#   - Touch the audit_log (that's the historical trail; keep it).
#   - Re-link documents to merchants (do that manually after re-parse).

$ErrorActionPreference = "Stop"

$BoxIP       = "5.161.51.105"
$BoxUser     = "root"
$KeyFile     = "C:/Users/fkozi/.ssh/aegis_ed25519"
$LocalPort   = 15555
$RemotePort  = 5555

# Pull bearer off the box (no echo in script history)
Write-Host "  fetching bearer token from box..." -ForegroundColor Cyan
$BearerToken = & ssh -i $KeyFile -o BatchMode=yes "$BoxUser@$BoxIP" "grep -E '^API_BEARER_TOKEN=' /etc/aegis/aegis.env | cut -d= -f2-"
if (-not $BearerToken) { throw "bearer missing on box" }
Write-Host ("  bearer: {0}... ({1} chars)" -f $BearerToken.Substring(0,8), $BearerToken.Length)

# Locate PDF folder
$onedrive = $env:OneDrive
if (-not $onedrive) { $onedrive = "$env:USERPROFILE\OneDrive" }
$desktopDir = Get-ChildItem -Path $onedrive -Directory | Where-Object { $_.Name -like "Radna*" } | Select-Object -First 1
if (-not $desktopDir) { throw "cannot find OneDrive desktop dir" }
$PdfFolder = Join-Path $desktopDir.FullName "bank statements"
$pdfs = @(Get-ChildItem -Path $PdfFolder -Filter "*.pdf" -File)
Write-Host ("  found {0} PDFs in {1}" -f $pdfs.Count, $PdfFolder)

# Confirmation prompt
Write-Host ""
Write-Host "=== ABOUT TO DELETE ALL existing document/transaction/analysis rows ===" -ForegroundColor Yellow
Write-Host "    Merchants will be KEPT. Audit log will be KEPT." -ForegroundColor Yellow
Write-Host "    PDFs will be re-uploaded fresh." -ForegroundColor Yellow
Write-Host ""
$confirm = Read-Host "Type YES (uppercase) to proceed"
if ($confirm -ne "YES") {
  Write-Host "  aborted by user." -ForegroundColor Red
  exit 1
}

# Step 1: delete via on-box python helper (uses service-key creds in /etc/aegis/aegis.env).
Write-Host ""
Write-Host "=== Step 1: wiping documents/transactions/analyses on Supabase ===" -ForegroundColor Cyan
$wipeScript = @'
sudo -u aegis bash -lc 'set -a; source /etc/aegis/aegis.env; set +a; cd /opt/aegis && /usr/local/bin/uv run python scripts/_reparse_wipe.py'
'@
$wipeResult = & ssh -i $KeyFile -o BatchMode=yes "$BoxUser@$BoxIP" $wipeScript 2>&1
Write-Host $wipeResult

# Step 2: open SSH tunnel and re-upload every PDF.
Write-Host ""
Write-Host "=== Step 2: re-upload all PDFs through SSH tunnel ===" -ForegroundColor Cyan
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

$uploaded = @()
try {
  foreach ($p in $pdfs) {
    Write-Host ""
    Write-Host ("==> {0}" -f $p.Name) -ForegroundColor Cyan
    $raw = & curl.exe -s -S -H "Authorization: Bearer $BearerToken" -F "file=@$($p.FullName)" --max-time 120 "http://127.0.0.1:$LocalPort/upload"
    try {
      $r = $raw | ConvertFrom-Json
      if ($r.document_id) {
        Write-Host ("    document_id={0}  parse_status={1}" -f $r.document_id, $r.parse_status) -ForegroundColor Green
        $uploaded += [pscustomobject]@{ name = $p.Name; document_id = $r.document_id }
      } else {
        Write-Host ("    upload returned no document_id: {0}" -f $raw) -ForegroundColor Red
      }
    } catch {
      Write-Host ("    raw response (not JSON): {0}" -f $raw) -ForegroundColor Red
    }
  }

  # Step 3: poll status until everything's out of pending.
  Write-Host ""
  Write-Host "=== Step 3: poll parse_status for ~60s per doc ===" -ForegroundColor Cyan
  $pollScript = "sudo -u aegis bash -lc 'cd /opt/aegis && set -a && source /etc/aegis/aegis.env && set +a && /usr/local/bin/uv run python scripts/_status_probe.py {0}'"
  foreach ($u in $uploaded) {
    for ($i = 1; $i -le 18; $i++) {
      Start-Sleep -Seconds 5
      $status = & ssh -i $KeyFile -o BatchMode=yes "$($BoxUser)@$($BoxIP)" ($pollScript -f $u.document_id) 2>$null
      if ($i -eq 18 -or $status -match "proceed|review|manual_review|error") {
        Write-Host ("  {0,-65} {1}" -f $u.name, ($status -join ""))
        break
      }
    }
  }
}
finally {
  Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
  Write-Host ""
  Write-Host "  tunnel closed"
}
