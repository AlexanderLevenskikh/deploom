# Pure release-version policy. Dot-sourcing this file never changes Git or files.
function ConvertTo-ReleaseVersion {
    param([Parameter(Mandatory = $true)][string]$Value)
    $Version = $Value.Trim()
    if ($Version.StartsWith('v')) { $Version = $Version.Substring(1) }
    if ($Version -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$') {
        throw "Unsupported SemVer version '$Value'."
    }
    $Parts = @($Matches[1], $Matches[2], $Matches[3], $Matches[4], $Matches[5])
    if ($Parts[3]) {
        foreach ($Identifier in ($Parts[3] -split '\.')) {
            if ($Identifier -cmatch '^[0-9]+$' -and $Identifier.Length -gt 1 -and $Identifier.StartsWith('0')) {
                throw "Numeric prerelease identifiers cannot have leading zeroes: '$Value'."
            }
        }
    }
    return [pscustomobject]@{ Version = $Version; Major = $Parts[0]; Minor = $Parts[1]; Patch = $Parts[2]; Prerelease = $Parts[3] }
}

function Get-NextReleaseTag {
    param([Parameter(Mandatory = $true)][string]$CurrentVersion)
    $Parsed = ConvertTo-ReleaseVersion $CurrentVersion
    $Core = "$($Parsed.Major).$($Parsed.Minor).$($Parsed.Patch)"
    if ($Parsed.Prerelease) {
        $Identifiers = @($Parsed.Prerelease -split '\.')
        $Last = $Identifiers.Length - 1
        if ($Identifiers[$Last] -cmatch '^[0-9]+$') {
            $Identifiers[$Last] = ([bigint]::Parse($Identifiers[$Last]) + 1).ToString()
        } else { $Identifiers += '1' }
        return "v$Core-$($Identifiers -join '.')"
    }
    return "v$($Parsed.Major).$($Parsed.Minor).$(([bigint]::Parse($Parsed.Patch) + 1).ToString())"
}

function Resolve-ReleaseTag {
    param([string]$CurrentVersion, [string]$Tag = '', [switch]$NonInteractive, [scriptblock]$ReadTag)
    if ([string]::IsNullOrWhiteSpace($Tag)) {
        $Suggested = Get-NextReleaseTag $CurrentVersion
        Write-Host "Suggested release tag: $Suggested"
        $Tag = $Suggested
        if ($ReadTag -or (-not $NonInteractive -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and -not $env:CI)) {
            $Entered = if ($ReadTag) { & $ReadTag $Suggested } else { Read-Host "Release tag [Enter = $Suggested]" }
            if (-not [string]::IsNullOrWhiteSpace($Entered)) { $Tag = $Entered }
        }
    }
    $Parsed = ConvertTo-ReleaseVersion $Tag
    return [pscustomobject]@{ Version = $Parsed.Version; Tag = "v$($Parsed.Version)" }
}
