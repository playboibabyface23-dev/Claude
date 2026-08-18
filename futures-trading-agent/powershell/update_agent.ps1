<#
.SYNOPSIS
    Updates the agent's code and Python dependencies. Does not start,
    stop, or restart the running process -- run restart_agent.ps1
    afterwards to apply the update.
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Push-Location $ProjectRoot
try {
    if (Test-Path (Join-Path $ProjectRoot ".git")) {
        Write-Host "Pulling latest changes..."
        git pull
    } else {
        Write-Host "Not a git checkout -- skipping code pull."
    }

    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

    Write-Host "Installing/upgrading dependencies with $PythonExe ..."
    & $PythonExe -m pip install --upgrade -r (Join-Path $ProjectRoot "requirements.txt")

    "$(Get-Date -Format o) update_agent.ps1 ran" | Add-Content -Path (Join-Path $LogDir "restarts.log")
    Write-Host "Update complete. Run restart_agent.ps1 to apply changes to a running agent."
}
finally {
    Pop-Location
}
