# Safe restart of the long-running web console (default 8848), e.g. to load
# new code. Use this instead of Stop-Process on the port.
#
# - Refuses while the server is busy: scheduler holding a project (render /
#   compose / prep), a publication uploading, or a team role running
#   (scripts/server_busy.py). -WaitMinutes N waits for it to go idle first.
# - Refuses when the server does not answer /api/health: a hung server is the
#   watchdog's job (ensure_server.ps1). -Force overrides both checks.
# - Refuses when another run.py exists besides the one on the port, because
#   ensure_server.ps1 would then not start a replacement.
# - Every restart and refusal is logged to data/logs/watchdog.log with -Reason,
#   so later readers can tell deliberate restarts from crashes.
# - Starts the new server through ensure_server.ps1 (same logs, same flags).
#
# Keep this file ASCII-only (PowerShell 5.1 reads .ps1 as ANSI). The Chinese
# explanation lives in README.md.
#
# Exit codes: 0 restarted, 1 busy, 2 cannot judge / precondition failed,
#             3 new server did not come up.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1 -Reason "load commit abc123"
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1 -Reason "..." -WaitMinutes 30
#   powershell -ExecutionPolicy Bypass -File scripts\restart_server.ps1 -Reason "..." -DryRun
param(
    [Parameter(Mandatory = $true)][string]$Reason,
    [int]$Port = 8848,
    [string]$Python = "python",
    [int]$WaitMinutes = 0,
    [int]$StartTimeoutSec = 90,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wlog = Join-Path $logDir "watchdog.log"

function Write-Log([string]$msg) {
    $line = "{0} restart: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
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

function Get-Busy([double]$Since) {
    $json = & $Python -I (Join-Path $PSScriptRoot 'server_busy.py') --port $Port --since $Since
    return @{ Code = $LASTEXITCODE; Json = ($json -join ' ') }
}

# 1. Who holds the port?
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listen) {
    Write-Log "port $Port has no server; nothing to stop, starting one (reason: $Reason)"
    if (-not $DryRun) { & (Join-Path $PSScriptRoot 'ensure_server.ps1') -Port $Port }
    exit 0
}
$oldPid = [int]$listen[0].OwningProcess
$proc = Get-CimInstance Win32_Process -Filter "ProcessId=$oldPid" -ErrorAction SilentlyContinue
if (-not $proc -or $proc.CommandLine -notmatch 'run\.py') {
    Write-Log "port $Port held by non-run.py pid $oldPid; refusing (reason: $Reason)"
    exit 2
}
$started = [DateTime]$proc.CreationDate   # CIM already returns a DateTime
$since = [double](([DateTimeOffset]$started).ToUnixTimeSeconds())

# 2. Another run.py besides this one? ensure_server.ps1 would refuse to start.
$others = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'run\.py' -and $_.ProcessId -ne $oldPid -and $_.ParentProcessId -ne $oldPid }
if ($others) {
    Write-Log "another run.py is running (pid $($others[0].ProcessId)); refusing, it would block the restart (reason: $Reason)"
    exit 2
}

# 3. Busy? Optionally wait.
if (-not $Force) {
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while ($true) {
        $b = Get-Busy $since
        if ($b.Code -eq 0) { break }
        if ($b.Code -eq 2) {
            Write-Log "pid $oldPid did not answer /api/health; refusing (hung servers are the watchdog's job; -Force to override) (reason: $Reason)"
            exit 2
        }
        if ((Get-Date) -ge $deadline) {
            Write-Log "pid $oldPid busy, not restarting: $($b.Json) (reason: $Reason)"
            exit 1
        }
        Start-Sleep -Seconds 30
    }
}

if ($DryRun) {
    Write-Log "DRY RUN: would stop pid $oldPid (started $started) and start a new server (reason: $Reason)"
    exit 0
}

# 4. Stop, wait for the port, start.
$flag = ''
if ($Force) { $flag = ' FORCE' }
Write-Log "stopping pid $oldPid (started $started)$flag (reason: $Reason)"
Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
for ($i = 0; $i -lt 40; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}
& (Join-Path $PSScriptRoot 'ensure_server.ps1') -Port $Port

$deadline = (Get-Date).AddSeconds($StartTimeoutSec)
while ((Get-Date) -lt $deadline) {
    if (Test-Healthy 5000) {
        $newPid = [int](Get-NetTCPConnection -LocalPort $Port -State Listen)[0].OwningProcess
        Write-Log "new server pid $newPid answering on port $Port"
        exit 0
    }
    Start-Sleep -Seconds 3
}
Write-Log "new server did not answer within ${StartTimeoutSec}s; see the newest data/logs/server-*.err.log"
exit 3
