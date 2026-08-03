<#
.SYNOPSIS
    Starts the futures trading agent as a background process and records
    its PID so restart_agent.ps1 / watchdog.ps1 / emergency_stop.ps1 can
    find it again.
.DESCRIPTION
    Refuses to start a second instance if agent.pid already points at a
    live process -- two instances of the same agent trading the same
    account is exactly the kind of thing the duplicate-order guard in
    execution/engine.py cannot fully protect against on its own.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$PidFile = Join-Path $LogDir "agent.pid"

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

if (Test-Path $PidFile) {
    $existingPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        Write-Host "Agent is already running (PID $existingPid)."
        Write-Host "Use restart_agent.ps1 if you need to restart it."
        exit 1
    }
}

Write-Host "Starting futures trading agent using $PythonExe ..."
$process = Start-Process -FilePath $PythonExe `
    -ArgumentList @("-m", "futures_agent.main") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput (Join-Path $LogDir "stdout.log") `
    -RedirectStandardError (Join-Path $LogDir "stderr.log") `
    -PassThru -WindowStyle Hidden

$process.Id | Out-File -FilePath $PidFile -Encoding ascii
"$(Get-Date -Format o) started PID $($process.Id) via start_agent.ps1" |
    Add-Content -Path (Join-Path $LogDir "restarts.log")

Write-Host "Agent started with PID $($process.Id)."
Write-Host "Logs: $LogDir (stdout.log, stderr.log, app.log, decisions.log, trades.log, ...)"
