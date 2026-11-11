[CmdletBinding()]
param(
    [string]$TaskName = "Kmart Last Mile daily refresh",
    [datetime]$At = (Get-Date).Date.AddHours(6),
    [switch]$WithInvoiceReconciliation,
    [switch]$RebuildHistory
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python runtime was not found at $pythonPath"
}

$arguments = "-m src.daily_refresh"
if ($WithInvoiceReconciliation) {
    $arguments += " --with-invoice-reconciliation"
}
if ($RebuildHistory) {
    $arguments += " --rebuild-history"
}

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Runs the Kmart CFC Last Mile 14-day operational refresh and incrementally persists live history." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' at $($At.ToString('HH:mm'))."
Write-Host "Command: $pythonPath $arguments"
