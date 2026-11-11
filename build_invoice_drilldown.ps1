param(
    [Parameter(Mandatory = $true)]
    [string]$InvoiceNumber,

    [ValidateSet("MOOREBANK", "TRUGANINA")]
    [string]$Site
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw "Could not find virtual environment Python at '$venvPython'."
}

$args = @(
    '-m', 'src.invoice_drilldown_report',
    '--invoice-number', $InvoiceNumber
)
if ($Site) {
    $args += @('--site', $Site)
}

Write-Host "Building invoice drilldown for $InvoiceNumber" -ForegroundColor Cyan
& $venvPython @args
