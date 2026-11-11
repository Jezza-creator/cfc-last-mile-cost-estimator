[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Low")]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $projectName = Split-Path -Leaf $resolvedRoot
    $OutputPath = Join-Path (Split-Path -Parent $resolvedRoot) "$projectName.zip"
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$excludedDirectories = @(".venv", ".github")

$items = Get-ChildItem -LiteralPath $resolvedRoot -Force | Where-Object {
    $_.Name -notin $excludedDirectories -and
    [IO.Path]::GetFullPath($_.FullName) -ne $resolvedOutput
}

if ($items.Count -eq 0) {
    throw "No files or folders were found to archive under '$resolvedRoot'."
}

$outputParent = Split-Path -Parent $resolvedOutput
if (-not (Test-Path -LiteralPath $outputParent)) {
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
}

if ($PSCmdlet.ShouldProcess($resolvedOutput, "Create project archive excluding $($excludedDirectories -join ', ')")) {
    if (Test-Path -LiteralPath $resolvedOutput) {
        Remove-Item -LiteralPath $resolvedOutput -Force
    }

    Compress-Archive -Path $items.FullName -DestinationPath $resolvedOutput -CompressionLevel Optimal -Force
    Write-Output "Created archive: $resolvedOutput"
    Write-Output "Excluded directories: $($excludedDirectories -join ', ')"
}
