<#
.SYNOPSIS
    Stops the running agent (if any) and starts a fresh instance.
.DESCRIPTION
    Use this after update_agent.ps1 pulls new code, or any time you need to
    apply a config change -- the agent only reads .env at startup.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$PidFile = Join-Path $LogDir "agent.pid"

if (Test-Path $PidFile) {
    $existingPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($existingPid) {
        $proc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "Stopping agent (PID $existingPid)..."
            Stop-Process -Id $existingPid -Force
            Start-Sleep -Seconds 2
        }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

"$(Get-Date -Format o) manual restart via restart_agent.ps1" |
    Add-Content -Path (Join-Path $LogDir "restarts.log")

& (Join-Path $PSScriptRoot "start_agent.ps1")
