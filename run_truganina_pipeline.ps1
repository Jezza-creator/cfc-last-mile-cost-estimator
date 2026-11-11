Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw "Could not find virtual environment Python at '$venvPython'."
}

Write-Host "Running Truganina pipeline from $repoRoot" -ForegroundColor Cyan

Write-Host "Stage 1: parcel assignment" -ForegroundColor Yellow
& $venvPython -m src.stage1_truganina_parcel_assigner

Write-Host "Stage 2: parcel physical estimation" -ForegroundColor Yellow
& $venvPython -m src.stage2_truganina_parcel_physical_estimator

Write-Host "Stage 3: cost estimation" -ForegroundColor Yellow
& $venvPython -m src.stage3_truganina_cost_estimator

Write-Host "Truganina pipeline completed successfully." -ForegroundColor Green
