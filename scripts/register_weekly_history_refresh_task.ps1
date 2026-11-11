[CmdletBinding()]
param(
    [string]$TaskName = "Kmart Last Mile weekly history refresh",
    [datetime]$At = (Get-Date).Date.AddHours(2)
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python runtime was not found at $pythonPath"
}

$arguments = "-m src.history_refresh"
$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Rebuilds the Kmart CFC Last Mile combined weekly history." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' every Monday at $($At.ToString('HH:mm'))."
Write-Host "Command: $pythonPath $arguments"
