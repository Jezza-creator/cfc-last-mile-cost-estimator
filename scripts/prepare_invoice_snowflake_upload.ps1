param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("allied", "gopeople", "couriers_please", "cp")]
    [string]$Carrier,

    [Parameter(Mandatory = $false)]
    [string]$InputFile,

    [Parameter(Mandatory = $false)]
    [string]$OutputRoot = "outputs\snowflake_upload"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$carrierKey = $Carrier.ToLowerInvariant()
if ($carrierKey -eq "cp") {
    $carrierKey = "couriers_please"
}

$inputDirectory = switch ($carrierKey) {
    "allied" { Join-Path $projectRoot "data\inputs\carrier_invoices\Allied" }
    "gopeople" { Join-Path $projectRoot "data\inputs\carrier_invoices\GoPeople" }
    "couriers_please" { Join-Path $projectRoot "data\inputs\carrier_invoices\couriers_please" }
    default { throw "Unsupported carrier: $Carrier" }
}

if (-not (Test-Path -LiteralPath $inputDirectory -PathType Container)) {
    throw "Expected invoice folder does not exist: $inputDirectory"
}

$workbooks = @(Get-ChildItem -LiteralPath $inputDirectory -File -Filter "*.xlsx" | Sort-Object Name)
if ($workbooks.Count -eq 0) {
    throw "No .xlsx invoice workbooks found in $inputDirectory"
}

if ($InputFile) {
    $inputPath = if ([IO.Path]::IsPathRooted($InputFile)) {
        [IO.Path]::GetFullPath($InputFile)
    } else {
        [IO.Path]::GetFullPath((Join-Path (Get-Location) $InputFile))
    }
    $inputRoot = ([IO.Path]::GetFullPath($inputDirectory)).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $inputPath.StartsWith($inputRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "InputFile must be inside the expected carrier folder: $inputDirectory"
    }
    if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) {
        throw "InputFile does not exist: $inputPath"
    }
}

$runId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$runDirectory = Join-Path (Join-Path $projectRoot $OutputRoot) ("{0}_{1}" -f $carrierKey, $runId)
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "The project virtual environment was not found at $python"
}

$module = switch ($carrierKey) {
    "allied" { "src.carrier_invoice_allied_ingest" }
    "gopeople" { "src.carrier_invoice_gopeople_ingest" }
    "couriers_please" { "src.carrier_invoice_cp_ingest" }
}

Push-Location $projectRoot
try {
    & $python -m $module --input-directory $inputDirectory --output-directory $runDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Invoice normalisation failed for $carrierKey with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$inputManifest = @(
    foreach ($workbook in $workbooks) {
        $hash = Get-FileHash -LiteralPath $workbook.FullName -Algorithm SHA256
        [ordered]@{
            file_name = $workbook.Name
            file_size_bytes = $workbook.Length
            last_write_time_utc = $workbook.LastWriteTimeUtc.ToString("o")
            sha256 = $hash.Hash
        }
    }
)

$outputManifest = @(
    Get-ChildItem -LiteralPath $runDirectory -File |
        Where-Object { $_.Name -ne "upload_manifest.json" } |
        Select-Object -ExpandProperty Name
)

$manifest = [ordered]@{
    load_id = $runId
    carrier_code = $carrierKey
    input_directory = $inputDirectory
    selected_input_file = if ($InputFile) { $inputPath } else { $null }
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    input_workbooks = $inputManifest
    generated_files = $outputManifest
    next_step = "Upload this package to the approved Snowflake landing stage and register load_id before running the table refresh."
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $runDirectory "upload_manifest.json") -Encoding UTF8

Write-Host "Invoice upload package created: $runDirectory"
Write-Host "Load manifest: $(Join-Path $runDirectory 'upload_manifest.json')"
Write-Host "Upload the generated CSV files and manifest to the approved Snowflake landing stage."
