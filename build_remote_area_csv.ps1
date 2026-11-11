param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$header = 'state,postcode,suburb,basic,per_kilogram'
$lines = Get-Content -LiteralPath $InputPath

if (-not $lines -or $lines.Count -lt 2) {
    throw "Input file does not contain enough rows."
}

$result = New-Object System.Collections.Generic.List[string]
$result.Add($header)

for ($i = 1; $i -lt $lines.Count; $i++) {
    $line = $lines[$i].Trim()
    if (-not $line) { continue }

    # Prefer tab-delimited rows from Excel/SharePoint. Fallback to whitespace parsing if needed.
    $parts = $line -split "`t"
    if ($parts.Count -lt 5) {
        $parts = $line -split '\s+'
        if ($parts.Count -ge 5) {
            $suburb = (($parts[2..($parts.Count - 3)] -join ' ').Trim()).ToUpper()
            $state = $parts[0].Trim().ToUpper()
            $postcode = $parts[1].Trim()
            $basic = $parts[-2] -replace '[\$,]', ''
            $perKg = $parts[-1] -replace '[\$,]', ''
            $result.Add("$state,$postcode,$suburb,$basic,$perKg")
            continue
        }
    }

    if ($parts.Count -ge 5 -and $parts[0] -match '^[A-Z]{2,3}$' -and $parts[1] -match '^\d{3,4}$') {
        $state = $parts[0].Trim().ToUpper()
        $postcode = $parts[1].Trim()
        $suburb = $parts[2].Trim().ToUpper()
        $basic = $parts[3] -replace '[\$,]', ''
        $perKg = $parts[4] -replace '[\$,]', ''

        $result.Add("$state,$postcode,$suburb,$basic,$perKg")
    }
    else {
        Write-Warning "Skipped unrecognized line: $line"
    }
}

Set-Content -LiteralPath $OutputPath -Value $result -Encoding UTF8

Write-Host "Wrote $($result.Count - 1) rows to $OutputPath"
