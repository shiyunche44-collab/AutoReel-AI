# Safe restart of the long-running console (default 8848), e.g. to load new code.
# Use this instead of Stop-Process.
#
# Two layouts (see ensure_server.ps1 and app/background.py):
#
# split (run.py --role web + run.py --role worker), -Target picks what to restart:
#   web     web console only. Never refused for being busy: it holds no renders,
#           uploads or team runs. Pages reload, nothing else is interrupted.
#   worker  background worker only. Refused while busy (a project lease held,
#           a publication uploading, a team role running; scripts/server_busy.py
#           --worker). Stopped gracefully through the stop-request file
#           data\<db>.worker.stop (renders stop between beats); hard-killed only
#           after StopTimeoutSec. Persistent jobs (jobs table) do not count as
#           busy: they are requeued and resumed.
#   all     (default) both; the worker busy check applies.
#
# single (old-style run.py without --role on the port): restarted as before
# (busy check through /api/health), and comes back as split unless -Mode single.
#
# - -WaitMinutes N waits for the worker / server to go idle first.
# - -Force skips the busy checks.
# - Every restart and refusal is logged to data/logs/watchdog.log with -Reason.
#
# Keep this file ASCII-only (PowerShell 5.1 reads .ps1 as ANSI). The Chinese
# explanation lives in README.md.
#
# Exit codes: 0 restarted, 1 busy, 2 cannot judge / precondition failed,
#             3 new process did not come up.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1 -Reason "load commit abc123"
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1 -Reason "..." -Target web
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1 -Reason "..." -WaitMinutes 30
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1 -Reason "..." -DryRun
param(
    [Parameter(Mandatory = $true)][string]$Reason,
    [int]$Port = 8848,
    [string]$Python = "python",
    [ValidateSet('all', 'web', 'worker')][string]$Target = 'all',
    [ValidateSet('split', 'single')][string]$Mode = 'split',
    [int]$WaitMinutes = 0,
    [int]$StartTimeoutSec = 90,
    [int]$StopTimeoutSec = 120,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wlog = Join-Path $logDir "watchdog.log"
$heartbeat = Join-Path $root "data\AutoReel-AI.db.worker.heartbeat"
$stopFile = Join-Path $root "data\AutoReel-AI.db.worker.stop"
$ensure = Join-Path $PSScriptRoot 'ensure_server.ps1'

function Write-Log([string]$msg) {
    $line = "{0} restart: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    # Write-Host, not Write-Output: output inside a function becomes part of its
    # return value (Wait-Up / Wait-Idle return exit codes).
    Write-Host $line
    if (-not $DryRun) { Add-Content -Path $wlog -Value $line -Encoding UTF8 }
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

function Get-Since($p) {
    $started = [DateTime]$p.CreationDate   # CIM already returns a DateTime
    return [double](([DateTimeOffset]$started).ToUnixTimeSeconds())
}

function Get-HeartbeatAt {
    if (-not (Test-Path $heartbeat)) { return 0 }
    try { return [double]((Get-Content -Path $heartbeat -Raw).Trim().Split(' ')[1]) } catch { return 0 }
}

# Waits until the busy check passes. $ArgsList are extra args for server_busy.py.
# Returns 0 idle, 1 busy (timed out), 2 cannot judge.
function Wait-Idle([string[]]$ArgsList, [string]$Who) {
    if ($Force) { return 0 }
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while ($true) {
        $json = & $Python -I (Join-Path $PSScriptRoot 'server_busy.py') @ArgsList
        $code = $LASTEXITCODE
        if ($code -eq 0) { return 0 }
        if ($code -eq 2) {
            Write-Log "$Who did not answer /api/health; refusing (hung servers are the watchdog's job; -Force to override) (reason: $Reason)"
            return 2
        }
        if ((Get-Date) -ge $deadline) {
            Write-Log "$Who busy, not restarting: $($json -join ' ') (reason: $Reason)"
            return 1
        }
        Start-Sleep -Seconds 30
    }
}

function Wait-PortFree {
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 500
    }
}

# Graceful worker stop: write the stop-request file, wait, hard-kill on timeout.
function Stop-Worker($w) {
    Write-Log "asking worker pid $($w.ProcessId) to stop (reason: $Reason)"
    Set-Content -Path $stopFile -Value "restart $(Get-Date -Format s)" -Encoding ASCII
    $deadline = (Get-Date).AddSeconds($StopTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $w.ProcessId -ErrorAction SilentlyContinue)) {
            Write-Log "worker pid $($w.ProcessId) stopped gracefully"
            return
        }
        Start-Sleep -Seconds 2
    }
    Write-Log "worker pid $($w.ProcessId) did not stop within ${StopTimeoutSec}s; killing"
    Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $stopFile -ErrorAction SilentlyContinue
}

function Wait-Up([bool]$NeedWeb, [bool]$NeedWorker, [double]$BeatBefore) {
    $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $webOk = (-not $NeedWeb) -or (Test-Healthy 5000)
        $workerOk = (-not $NeedWorker) -or ((Get-HeartbeatAt) -gt $BeatBefore)
        if ($webOk -and $workerOk) {
            Write-Log "back up (web: $NeedWeb, worker: $NeedWorker) on port $Port"
            return 0
        }
        Start-Sleep -Seconds 3
    }
    Write-Log "not up within ${StartTimeoutSec}s (web wanted: $NeedWeb, worker wanted: $NeedWorker); see the newest data/logs/server-*.err.log / worker-*.err.log"
    return 3
}

# ---------------------------------------------------------------------------
# Who is running?
# ---------------------------------------------------------------------------
$procs = Get-RunPy
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$webProc = $null
if ($listen) {
    $webPid = [int]$listen[0].OwningProcess
    $webProc = Get-CimInstance Win32_Process -Filter "ProcessId=$webPid" -ErrorAction SilentlyContinue
    if (-not $webProc -or $webProc.CommandLine -notmatch 'run\.py') {
        Write-Log "port $Port held by non-run.py pid $webPid; refusing (reason: $Reason)"
        exit 2
    }
}
$workers = @($procs | Where-Object { $_.CommandLine -match '--role\s+worker' })
$isSingle = $webProc -and ($webProc.CommandLine -notmatch '--role\s+(web|worker)')

# ---------------------------------------------------------------------------
# Old-style single process on the port: restart it as a whole (as before).
# ---------------------------------------------------------------------------
if ($isSingle) {
    $oldPid = [int]$webProc.ProcessId
    $others = @($procs | Where-Object { $_.ProcessId -ne $oldPid -and $_.ParentProcessId -ne $oldPid })
    if ($others.Count -gt 0) {
        Write-Log "another run.py is running (pid $($others[0].ProcessId)); refusing, it would block the restart (reason: $Reason)"
        exit 2
    }
    $code = Wait-Idle @('--port', "$Port", '--since', "$(Get-Since $webProc)") "pid $oldPid"
    if ($code -ne 0) { exit $code }
    if ($DryRun) {
        Write-Log "DRY RUN: would stop single-process pid $oldPid and start mode $Mode (reason: $Reason)"
        exit 0
    }
    $flag = ''
    if ($Force) { $flag = ' FORCE' }
    Write-Log "stopping single-process pid $oldPid$flag; coming back as mode $Mode (reason: $Reason)"
    $beat = Get-HeartbeatAt
    Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    Wait-PortFree
    & $ensure -Port $Port -Mode $Mode -Python $Python
    exit (Wait-Up $true ($Mode -eq 'split') $beat)
}

# ---------------------------------------------------------------------------
# Split layout.
# ---------------------------------------------------------------------------
$doWeb = ($Target -eq 'all' -or $Target -eq 'web')
$doWorker = ($Target -eq 'all' -or $Target -eq 'worker')

if ($doWorker -and $workers.Count -gt 0) {
    $code = Wait-Idle @('--worker', '--since', "$(Get-Since $workers[0])") "worker pid $($workers[0].ProcessId)"
    if ($code -ne 0) { exit $code }
}

if ($DryRun) {
    $wp = 'none'
    if ($webProc) { $wp = $webProc.ProcessId }
    $kp = 'none'
    if ($workers.Count -gt 0) { $kp = $workers[0].ProcessId }
    Write-Log "DRY RUN: target $Target; would restart web pid $wp ($doWeb) and worker pid $kp ($doWorker) (reason: $Reason)"
    exit 0
}

$beat = Get-HeartbeatAt
if ($doWorker -and $workers.Count -gt 0) { Stop-Worker $workers[0] }
if ($doWeb -and $webProc) {
    Write-Log "stopping web pid $($webProc.ProcessId) (reason: $Reason)"
    Stop-Process -Id $webProc.ProcessId -Force -ErrorAction SilentlyContinue
    Wait-PortFree
}
& $ensure -Port $Port -Mode 'split' -Python $Python
exit (Wait-Up $doWeb $doWorker $beat)
