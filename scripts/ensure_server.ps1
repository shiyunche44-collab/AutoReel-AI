# Watchdog: start the web console ONLY if nothing is serving it.
#
# Does not kill a server that answers (unlike serve.ps1). One DB = one server,
# so this also skips when any other `run.py` python process exists.
#
# Exception: a run.py that holds the port but fails every probe for
# ~HangProbes x 30s is hung (rendering runs in worker threads and never blocks
# the event loop), so it is killed and restarted. 2026-11-13 a hung 8848 sat
# for hours because this script only logged it. -NoRestart disables that.
# Keep this file ASCII-only (PowerShell 5.1 reads .ps1 as ANSI). The Chinese
# explanation lives in README.md.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\ensure_server.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ensure_server.ps1 -Port 8899 -DryRun
param(
    [int]$Port = 8848,
    [string]$Python = "python",
    [int]$HangProbes = 3,
    [switch]$NoRestart,
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

function Test-Healthy([int]$TimeoutMs) {
    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/")
        $req.Proxy = $null
        $req.Timeout = $TimeoutMs
        $resp = $req.GetResponse()
        $ok = ([int]$resp.StatusCode -eq 200)
        $resp.Close()
        return $ok
    } catch { return $false }
}

# 1. Healthy already? (no proxy, short timeout)
if (Test-Healthy 5000) { exit 0 }

# 2. Port held but not answering. Probe again with long timeouts; if it never
#    answers, it is hung (not busy) -> kill it and fall through to start.
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    $hungPid = [int]$listen[0].OwningProcess
    for ($i = 1; $i -le $HangProbes; $i++) {
        if (Test-Healthy 30000) {
            Write-Log "port $Port answered on slow probe $i (pid $hungPid); busy, not hung"
            exit 0
        }
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$hungPid" -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.CommandLine -notmatch 'run\.py') {
        Write-Log "port $Port held by non-run.py pid $hungPid and not answering; leaving it alone"
        exit 0
    }
    if ($NoRestart -or $DryRun) {
        Write-Log "port $Port hung (pid $hungPid, $HangProbes probes x 30s failed); NoRestart/DryRun, leaving it"
        exit 0
    }
    Write-Log "port $Port hung (pid $hungPid, $HangProbes probes x 30s failed); killing and restarting"
    Stop-Process -Id $hungPid -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
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
