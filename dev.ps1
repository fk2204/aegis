# dev.ps1 — start AEGIS locally on Windows without manual env gymnastics.
#
# Usage:
#   .\dev.ps1                 # default: 127.0.0.1:8080 with --reload
#   .\dev.ps1 -Port 8000      # override port
#   .\dev.ps1 -NoReload       # disable --reload (useful when profiling)
#
# Delegates to scripts\dev.py which handles BOM + UTF-16-LE .env
# encoding (the default when the file was written through PowerShell
# redirection). ``uv --env-file`` uses a strict dotenv parser that
# chokes on UTF-16, hence the Python-side helper.

param(
    [int]$Port = 8080,
    [switch]$NoReload
)

$env:AEGIS_DEV_PORT = "$Port"
if ($NoReload) {
    $env:AEGIS_DEV_NORELOAD = "1"
} else {
    Remove-Item Env:AEGIS_DEV_NORELOAD -ErrorAction SilentlyContinue
}

$script = Join-Path $PSScriptRoot "scripts\dev.py"
if (-not (Test-Path $script)) {
    Write-Error "scripts\dev.py not found at $script"
    exit 1
}

uv run python $script
