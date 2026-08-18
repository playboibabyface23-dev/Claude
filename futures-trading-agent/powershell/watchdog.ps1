<#
.SYNOPSIS
    Supervises the agent process: restarts it automatically if it exits
    unexpectedly, and records every restart to logs/restarts.log.
.DESCRIPTION
    Intended to run continuously in its own window (or as a Scheduled Task
    that runs this script at logon). Does two things a crashed process
    cannot do for itself: notice it's gone, and cap how often it gets
    relaunched -- a process that dies every few seconds because of a real
    configuration problem should stop being restarted and instead surface
    that loudly, not spin forever.

    Respects the kill switch: if logs/KILL_SWITCH exists, the watchdog will
    NOT restart the agent, even if it's not running. Delete the file (or
    run emergency_stop.ps1's counterpart of clearing it) to resume.
.PARAMETER CheckIntervalSeconds
    How often to check whether the agent is still running.
.PARAMETER MaxRestartsPerHour
    Safety cap: once this many restarts have happened in the trailing hour,
    the watchdog stops relaunching automatically and just logs the fact
    until an operator intervenes.
#>
param(
    [int]$CheckIntervalSeconds = 15,
    [int]$MaxRestartsPerHour = 6
)

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$PidFile = Join-Path $LogDir "agent.pid"
$KillSwitchFile = Join-Path $LogDir "KILL_SWITCH"
$RestartLog = Join-Path $LogDir "restarts.log"

function Write-RestartLog {
    param([string]$Message)
    "$(Get-Date -Format o) $Message" | Add-Content -Path $RestartLog
    Write-Host $Message
}

function Test-AgentRunning {
    if (-not (Test-Path $PidFile)) { return $false }
    $existingPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if (-not $existingPid) { return $false }
    return [bool](Get-Process -Id $existingPid -ErrorAction SilentlyContinue)
}

Write-RestartLog "watchdog started (interval=${CheckIntervalSeconds}s, max ${MaxRestartsPerHour} restarts/hour)"

$restartTimestamps = @()

while ($true) {
    Start-Sleep -Seconds $CheckIntervalSeconds

    if (Test-Path $KillSwitchFile) {
        Write-RestartLog "kill switch is active -- watchdog will not (re)start the agent"
        continue
    }

    if (Test-AgentRunning) {
        continue
    }

    $now = Get-Date
    $restartTimestamps = @($restartTimestamps | Where-Object { $_ -gt $now.AddHours(-1) })

    if ($restartTimestamps.Count -ge $MaxRestartsPerHour) {
        Write-RestartLog ("agent is down but $MaxRestartsPerHour restarts already happened in " +
                         "the last hour -- not auto-restarting again. Investigate, then run " +
                         "start_agent.ps1 by hand once the cause is fixed.")
        continue
    }

    Write-RestartLog "agent process not found -- restarting"
    $restartTimestamps += $now
    & (Join-Path $PSScriptRoot "start_agent.ps1")
}
