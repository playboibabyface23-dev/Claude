<#
.SYNOPSIS
    Immediately halts new trading and stops the agent process.
.DESCRIPTION
    Creates the kill switch file risk/manager.py checks on every decision,
    so even a running agent refuses any new order the moment it next checks
    -- then stops the process itself, and stops the watchdog from
    restarting it (the watchdog also checks the kill switch file).

    This does NOT close any open broker position. Positions already open at
    Tradovate/TradersPost are untouched -- close them manually if required.
#>

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$PidFile = Join-Path $LogDir "agent.pid"
$KillSwitchFile = Join-Path $LogDir "KILL_SWITCH"
$RestartLog = Join-Path $LogDir "restarts.log"

"tripped: manual emergency stop via emergency_stop.ps1 at $(Get-Date -Format o)" |
    Set-Content -Path $KillSwitchFile
Write-Host "Kill switch file created: $KillSwitchFile"

if (Test-Path $PidFile) {
    $existingPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        Write-Host "Stopping agent process (PID $existingPid)..."
        Stop-Process -Id $existingPid -Force
    }
}

"$(Get-Date -Format o) emergency stop invoked" | Add-Content -Path $RestartLog

Write-Host ""
Write-Host "Agent stopped and kill switch is active."
Write-Host "IMPORTANT: open broker positions are NOT closed automatically --"
Write-Host "verify and close them manually in Tradovate/TradersPost if needed."
Write-Host ""
Write-Host "To resume trading: delete $KillSwitchFile, then run start_agent.ps1"
