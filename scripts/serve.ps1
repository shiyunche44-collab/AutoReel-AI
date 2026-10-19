# Launch the web console as a fully detached process.
#
# Why this exists: running `python run.py` as a managed background task makes
# the server a CHILD of that task. Managed tasks have a 1-hour ceiling and the
# server gets torn down with them -- which looks exactly like "the server
# mysteriously died", when in fact nothing crashed. Start-Process detaches it,
# so it survives.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files as
# ANSI unless they carry a UTF-8 BOM, so non-ASCII comments here get mangled
# into mojibake and break the parser. The Chinese explanation lives in
# README.md instead.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\serve.ps1 -Port 9000
param(
    [int]$Port = 8860,
    [string]$Python = "python"
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $logDir "server-$stamp.out.log"
$err = Join-Path $logDir "server-$stamp.err.log"

# Free the port first, otherwise a second copy starts silently on another port.
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($l in $listeners) {
    Write-Output "Port $Port is held by PID $($l.OwningProcess); stopping it"
    Stop-Process -Id $l.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
}

$env:PYTHONIOENCODING = 'utf-8'
$proc = Start-Process -FilePath $Python `
    -ArgumentList @('-X', 'utf8', (Join-Path $root 'run.py'), '--port', $Port) `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -PassThru

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        $ready = $true
        break
    }
    if ($proc.HasExited) { break }
}

if ($ready) {
    Write-Output "Server up. PID=$($proc.Id)  http://127.0.0.1:$Port"
    Write-Output "Logs: $out"
} else {
    Write-Output "Failed to start (PID=$($proc.Id))"
    if (Test-Path $err) { Get-Content $err -Tail 20 }
    exit 1
}
