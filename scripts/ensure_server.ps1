# Watchdog: make sure the web console AND the background worker are running.
#
# Default -Mode split runs two processes (see app/background.py):
#   run.py --port <Port> --role web   web console only; safe to restart any time
#   run.py --role worker              scheduler, jobs, publishing, metrics, AI team
# -Mode single keeps the old layout: one run.py doing both (no --role).
#
# Web part (both modes):
# - Does not kill a server that answers (unlike serve.ps1).
# - A run.py that holds the port but fails every probe for ~HangProbes x 30s is
#   hung, so it is killed and restarted. -NoRestart disables that.
# - single mode only: skips starting when any other run.py exists (one DB = one
#   scheduler). In split mode the web process runs no scheduler, so it may start.
#
# Worker part (split mode only):
# - If an old-style run.py (no --role) is running, it already owns the background
#   loops: do nothing. restart_server.ps1 replaces it with web + worker.
# - No worker process -> start one.
# - Worker alive but its heartbeat file (data\<db>.worker.heartbeat, written every
#   10s from its event loop) is older than WorkerStaleSec -> hung: kill, restart.
#
# Keep this file ASCII-only (PowerShell 5.1 reads .ps1 as ANSI). The Chinese
# explanation lives in README.md.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\ensure_server.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\ensure_server.ps1 -Port 8899 -DryRun
#   powershell -ExecutionPolicy Bypass -File scripts\ensure_server.ps1 -Mode single
param(
    [int]$Port = 8848,
    [string]$Python = "python",
    [int]$HangProbes = 3,
    [ValidateSet('split', 'single')][string]$Mode = 'split',
    [int]$WorkerStaleSec = 180,
    [switch]$NoRestart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wlog = Join-Path $logDir "watchdog.log"
$heartbeat = Join-Path $root "data\AutoReel-AI.db.worker.heartbeat"

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    # Write-Host, not Write-Output: output inside a function becomes part of its return value
    if ($DryRun) { Write-Host $line } else { Add-Content -Path $wlog -Value $line -Encoding UTF8 }
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

function Get-RunPy {
    $all = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    return @($all | Where-Object { $_.CommandLine -match 'run\.py' })
}

function Test-IsWorker($p) { return ($p.CommandLine -match '--role\s+worker') }
function Test-IsSingle($p) { return ($p.CommandLine -notmatch '--role\s+(web|worker)') }

function Start-Detached([string[]]$ArgList, [string]$Tag) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $out = Join-Path $logDir "$Tag-$stamp.out.log"
    $err = Join-Path $logDir "$Tag-$stamp.err.log"
    if ($DryRun) {
        Write-Log "DRY RUN: would start: $Python $($ArgList -join ' ')  (logs $out)"
        return $null
    }
    $env:PYTHONIOENCODING = 'utf-8'
    $p = Start-Process -FilePath $Python -ArgumentList $ArgList -WorkingDirectory $root `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    return $p
}

function Get-HeartbeatAge {
    # Seconds since the worker last wrote its heartbeat; -1 when there is none.
    if (-not (Test-Path $heartbeat)) { return -1 }
    try {
        $parts = (Get-Content -Path $heartbeat -Raw).Trim().Split(' ')
        $at = [double]$parts[1]
        $now = [double]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
        return [int]($now - $at)
    } catch { return -1 }
}

# ---------------------------------------------------------------------------
# 1. Web console on $Port
# ---------------------------------------------------------------------------
function Ensure-Web {
    if (Test-Healthy 5000) { return }

    # Port held but not answering: probe with long timeouts; if it never answers,
    # it is hung (not busy) -> kill it and fall through to start.
    $listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listen) {
        $hungPid = [int]$listen[0].OwningProcess
        for ($i = 1; $i -le $HangProbes; $i++) {
            if (Test-Healthy 30000) {
                Write-Log "port $Port answered on slow probe $i (pid $hungPid); busy, not hung"
                return
            }
        }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$hungPid" -ErrorAction SilentlyContinue
        if (-not $proc -or $proc.CommandLine -notmatch 'run\.py') {
            Write-Log "port $Port held by non-run.py pid $hungPid and not answering; leaving it alone"
            return
        }
        if ($NoRestart -or $DryRun) {
            Write-Log "port $Port hung (pid $hungPid, $HangProbes probes x 30s failed); NoRestart/DryRun, leaving it"
            return
        }
        Write-Log "port $Port hung (pid $hungPid, $HangProbes probes x 30s failed); killing and restarting"
        Stop-Process -Id $hungPid -Force -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 20; $i++) {
            if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 500
        }
    }

    if ($Mode -eq 'single') {
        # Another server on the same DB (dev/e2e on another port)? Do not start a second scheduler.
        $others = @(Get-RunPy | Where-Object { -not (Test-IsWorker $_) })
        if ($others.Count -gt 0) {
            Write-Log "another run.py is running (pid $($others[0].ProcessId)); not starting a second server"
            return
        }
        $argList = @('-X', 'utf8', (Join-Path $root 'run.py'), '--port', "$Port")
    } else {
        $argList = @('-X', 'utf8', (Join-Path $root 'run.py'), '--port', "$Port", '--role', 'web')
    }
    $p = Start-Detached $argList 'server'
    if ($p) { Write-Log "started web pid $($p.Id) on port $Port (mode $Mode)" }
}

# ---------------------------------------------------------------------------
# 2. Background worker (split mode)
# ---------------------------------------------------------------------------
function Ensure-Worker {
    $procs = Get-RunPy
    $single = @($procs | Where-Object { Test-IsSingle $_ })
    if ($single.Count -gt 0) {
        # An old-style run.py owns the background loops already.
        return
    }
    $workers = @($procs | Where-Object { Test-IsWorker $_ })
    if ($workers.Count -gt 0) {
        $w = $workers[0]
        $age = Get-HeartbeatAge
        $upSec = [int]((Get-Date) - [DateTime]$w.CreationDate).TotalSeconds
        if ($upSec -lt $WorkerStaleSec) { return }   # just started; give it time
        if ($age -ge 0 -and $age -lt $WorkerStaleSec) { return }
        if ($NoRestart -or $DryRun) {
            Write-Log "worker pid $($w.ProcessId) heartbeat stale (age $age s); NoRestart/DryRun, leaving it"
            return
        }
        Write-Log "worker pid $($w.ProcessId) heartbeat stale (age $age s, up $upSec s); killing and restarting"
        Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    $argList = @('-X', 'utf8', (Join-Path $root 'run.py'), '--role', 'worker')
    $p = Start-Detached $argList 'worker'
    if ($p) { Write-Log "started worker pid $($p.Id)" }
}

Ensure-Web
if ($Mode -eq 'split') { Ensure-Worker }
exit 0
