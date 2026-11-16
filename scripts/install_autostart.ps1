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

# Launch through wscript + a VBS wrapper: powershell.exe flashes a console
# window on every run even with -WindowStyle Hidden.
$script = Join-Path $PSScriptRoot 'ensure_server_hidden.vbs'
$root = Split-Path -Parent $PSScriptRoot
$arg = "//B //Nologo `"$script`" $Port"
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg -WorkingDirectory $root

# Two triggers: at logon, plus a time trigger that starts now and repeats
# forever. The repetition used to hang off the logon trigger only, so the task
# never ran until the next logon (2026-11-15 8848 stayed down a whole day).
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$trigger = @($logon, $every)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Registered task $name (logon + every $EveryMinutes min, port $Port)"
