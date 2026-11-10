# Register (or remove) a Task Scheduler job that keeps the web console alive:
# runs ensure_server.ps1 at logon and every 5 minutes after.
# Keep this file ASCII-only. Chinese explanation lives in README.md.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
param(
    [int]$Port = 8848,
    [int]$EveryMinutes = 5,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$name = 'AutoReel-AI-Server'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed task $name"
    exit 0
}

$script = Join-Path $PSScriptRoot 'ensure_server.ps1'
$root = Split-Path -Parent $PSScriptRoot
$arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Port $Port"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Repeat indefinitely after logon.
$rep = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$trigger.Repetition = $rep.Repetition

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Registered task $name (logon + every $EveryMinutes min, port $Port)"
