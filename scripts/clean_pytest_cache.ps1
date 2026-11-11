[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Low")]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [switch]$CleanUserTemp
)

$ErrorActionPreference = "Stop"

function Get-NormalizedPath {
    param([Parameter(Mandatory)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
}

function Test-ChildPath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Parent
    )

    $child = Get-NormalizedPath $Path
    $parent = Get-NormalizedPath $Parent
    return $child.StartsWith("$parent\", [System.StringComparison]::OrdinalIgnoreCase)
}

function Remove-PytestDirectory {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$AllowedParent
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not (Test-ChildPath -Path $resolved -Parent $AllowedParent)) {
        throw "Refusing to remove a path outside the approved directory: $resolved"
    }

    if ($PSCmdlet.ShouldProcess($resolved, "Remove pytest cache directory")) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host "Removed $resolved"
    }
}

$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$projectRootPath = Get-NormalizedPath $resolvedProjectRoot

$fixedTargets = @(
    (Join-Path $projectRootPath ".pytest_cache"),
    (Join-Path $projectRootPath ".pytest-tmp")
)

foreach ($target in $fixedTargets) {
    Remove-PytestDirectory -Path $target -AllowedParent $projectRootPath
}

$generatedTargets = Get-ChildItem -LiteralPath $projectRootPath -Directory -Force |
    Where-Object { $_.Name -like "pytest-cache-files-*" }
foreach ($target in $generatedTargets) {
    Remove-PytestDirectory -Path $target.FullName -AllowedParent $projectRootPath
}

if ($CleanUserTemp) {
    $tempRoot = Get-NormalizedPath ([System.IO.Path]::GetTempPath())
    $userTempName = "pytest-of-$env:USERNAME"
    $pytestTempRoot = Join-Path $tempRoot $userTempName

    if (Test-Path -LiteralPath $pytestTempRoot) {
        $resolvedTempRoot = (Resolve-Path -LiteralPath $pytestTempRoot).Path
        if ((Split-Path -Leaf $resolvedTempRoot) -ne $userTempName) {
            throw "Refusing to remove an unexpected pytest temp path: $resolvedTempRoot"
        }
        Remove-PytestDirectory -Path $resolvedTempRoot -AllowedParent $tempRoot
    }
}

Write-Host "Pytest cache cleanup complete."
