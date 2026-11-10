# Watchdog: start the web console ONLY if nothing is serving it.
#
# Never kills anything (unlike serve.ps1). One DB = one server, so this also
# skips when any other `run.py` python process exists (e.g. a dev server).
# Keep this file ASCII-only (PowerShell 5.1 reads .ps1 as ANSI). The Chinese
# explanation lives in README.md.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\ensure_server.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ensure_server.ps1 -Port 8899 -DryRun
param(
    [int]$Port = 8848,
    [string]$Python = "python",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wlog = Join-Path $logDir "watchdog.log"

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    if ($DryRun) { Write-Output $line } else { Add-Content -Path $wlog -Value $line -Encoding UTF8 }
}

# 1. Healthy already? (no proxy, short timeout)
$healthy = $false
try {
    $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/")
    $req.Proxy = $null
    $req.Timeout = 5000
    $resp = $req.GetResponse()
    $healthy = ([int]$resp.StatusCode -eq 200)
    $resp.Close()
} catch { $healthy = $false }
if ($healthy) { exit 0 }

# 2. Port held but not answering: hung or busy. Do NOT kill (may be mid-render).
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Log "port $Port is listening but did not answer in 5s (pid $($listen[0].OwningProcess)); leaving it alone"
    exit 0
}

# 3. Another server on the same DB (dev/e2e on another port)? Do not start a second scheduler.
$others = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'run\.py' }
if ($others) {
    Write-Log "another run.py is running (pid $($others[0].ProcessId)); not starting a second server"
    exit 0
}

# 4. Start detached, same as serve.ps1.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $logDir "server-$stamp.out.log"
$err = Join-Path $logDir "server-$stamp.err.log"
$args2 = @('-X', 'utf8', (Join-Path $root 'run.py'), '--port', $Port)
if ($DryRun) {
    Write-Log "DRY RUN: would start: $Python $($args2 -join ' ')  (logs $out)"
    exit 0
}
$env:PYTHONIOENCODING = 'utf-8'
$proc = Start-Process -FilePath $Python -ArgumentList $args2 -WorkingDirectory $root `
    -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
Write-Log "started server pid $($proc.Id) on port $Port"
