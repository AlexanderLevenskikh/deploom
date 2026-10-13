# Windows-only, isolated VHDX COW viability benchmark. Does not read or modify a user checkout.
# Run from an elevated PowerShell with the Hyper-V PowerShell module installed.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ScratchRoot,
    [ValidateRange(10,50000)][int]$Files = 1000,
    [ValidateRange(1,1024)][int]$FileKB = 64,
    [ValidateRange(1,20)][int]$Trials = 3,
    [switch]$KeepArtifacts
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

foreach ($command in @('New-VHD','Mount-VHD','Dismount-VHD','Get-Disk','Get-Partition')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "VHDX_BACKEND_UNAVAILABLE: $command is unavailable; install Hyper-V PowerShell tools. No fallback is implied."
    }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'VHDX_BACKEND_REQUIRES_ELEVATION: benchmark is disabled without administrative privileges.'
}
$baseRoot = [IO.Path]::GetFullPath($ScratchRoot)
if (-not (Test-Path -LiteralPath $baseRoot -PathType Container)) {
    throw "SCRATCH_ROOT_MISSING: $baseRoot (create a dedicated scratch folder before running)"
}
$session = Join-Path $baseRoot ("deploom-vhdx-poc-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $session -ErrorAction Stop | Out-Null
$parentPath = Join-Path $session 'base.vhdx'
$mounts = New-Object 'System.Collections.Generic.List[string]'
$measurements = New-Object 'System.Collections.Generic.List[object]'
$success = $false

function Mount-BenchmarkDisk([string]$path) {
    $vhd = Mount-VHD -Path $path -Passthru -ErrorAction Stop
    $mounts.Add($path)
    $disk = $vhd | Get-Disk
    if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false -ErrorAction Stop }
    if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false -ErrorAction Stop }
    return $disk
}
function Get-BenchmarkRoot($disk) {
    $part = Get-Partition -DiskNumber $disk.Number |
        Where-Object { $_.Type -ne 'Reserved' -and $_.Size -gt 1MB } |
        Sort-Object Size -Descending | Select-Object -First 1
    if (-not $part) { throw 'VHDX_PARTITION_MISSING' }
    if (-not $part.DriveLetter) {
        $part | Add-PartitionAccessPath -AssignDriveLetter -ErrorAction Stop
        $part = Get-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber
    }
    if (-not $part.DriveLetter) { throw 'VHDX_DRIVE_LETTER_UNAVAILABLE' }
    return "$(($part.DriveLetter)):\"
}
function Unmount-BenchmarkDisk([string]$path) {
    Dismount-VHD -Path $path -ErrorAction Stop
    [void]$mounts.Remove($path)
}

try {
    $payload = New-Object byte[] ($FileKB * 1024)
    ([System.Random]::new(42)).NextBytes($payload)
    $baseCreated = Measure-Command {
        New-VHD -Path $parentPath -Dynamic -SizeBytes ([math]::Max(4GB, [long]($Files * [long]$FileKB * 1024 * 1.25 + 1GB))) -ErrorAction Stop | Out-Null
        $disk = Mount-BenchmarkDisk $parentPath
        $disk = Initialize-Disk -Number $disk.Number -PartitionStyle GPT -PassThru -ErrorAction Stop
        $part = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
        Format-Volume -Partition $part -FileSystem NTFS -NewFileSystemLabel 'DEPLOOM-COW-POC' -Confirm:$false -ErrorAction Stop | Out-Null
        $root = Get-BenchmarkRoot $disk
        [IO.File]::WriteAllText((Join-Path $root 'sentinel.txt'), 'SEALED-PARENT', [Text.Encoding]::UTF8)
        $assets = Join-Path $root 'packages'
        [IO.Directory]::CreateDirectory($assets) | Out-Null
        for ($i = 0; $i -lt $Files; $i++) {
            [IO.File]::WriteAllBytes((Join-Path $assets ("asset-{0:D6}.bin" -f $i)), $payload)
        }
        Unmount-BenchmarkDisk $parentPath
    }
    $parentHash = (Get-FileHash -LiteralPath $parentPath -Algorithm SHA256).Hash
    $measurements.Add([pscustomobject]@{stage='parent-initialization';trial=0;milliseconds=$baseCreated.TotalMilliseconds;bytes=(Get-Item $parentPath).Length})

    for ($index=1; $index -le $Trials; $index++) {
        $childPath = Join-Path $session ("trial-{0:D2}.vhdx" -f $index)
        $create = Measure-Command { New-VHD -Path $childPath -ParentPath $parentPath -Differencing -ErrorAction Stop | Out-Null }
        $attach = Measure-Command {
            $disk = Mount-BenchmarkDisk $childPath
            $childRoot = Get-BenchmarkRoot $disk
            $observed = [IO.File]::ReadAllText((Join-Path $childRoot 'sentinel.txt'), [Text.Encoding]::UTF8)
            if ($observed -ne 'SEALED-PARENT') { throw "CHILD_ISOLATION_BROKEN: trial=$index actual=$observed" }
        }
        $write = Measure-Command {
            [IO.File]::WriteAllText((Join-Path $childRoot 'sentinel.txt'), "CHILD-$index", [Text.Encoding]::UTF8)
            [IO.File]::WriteAllBytes((Join-Path $childRoot "trial-output-$index.bin"), $payload)
        }
        $detach = Measure-Command { Unmount-BenchmarkDisk $childPath }
        $afterHash = (Get-FileHash -LiteralPath $parentPath -Algorithm SHA256).Hash
        if ($parentHash -ne $afterHash) { throw "VHDX_PARENT_MUTATED: after child $index" }
        $measurements.Add([pscustomobject]@{
            stage='differencing-trial';trial=$index
            createMs=[math]::Round($create.TotalMilliseconds,2)
            attachMs=[math]::Round($attach.TotalMilliseconds,2)
            writeMs=[math]::Round($write.TotalMilliseconds,2)
            detachMs=[math]::Round($detach.TotalMilliseconds,2)
            childPhysicalBytes=(Get-Item $childPath).Length
        })
    }
    $success = $true
} finally {
    for ($i=$mounts.Count-1; $i -ge 0; $i--) {
        try { Dismount-VHD -Path $mounts[$i] -ErrorAction Stop } catch {
            Write-Warning "ORPHANED_VHDX_MOUNT: $($mounts[$i]); clean up manually before deleting artifacts: $_"
            $KeepArtifacts = $true
        }
    }
    $report = [pscustomobject]@{
        schema='deploom-vhdx-cow-poc-v1';success=$success
        host=[Environment]::OSVersion.VersionString
        files=$Files;fileKB=$FileKB;trials=$Trials
        scratchPath=$session;measurements=$measurements.ToArray()
        limitation='Synthetic Windows-only experiment, not proof of product compatibility or security isolation'
    }
    $reportPath = Join-Path $baseRoot ('deploom-vhdx-poc-' + [IO.Path]::GetFileName($session) + '.json')
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host "VHDX PoC report: $reportPath"
    if (-not $KeepArtifacts) {
        $resolvedSession = [IO.Path]::GetFullPath($session)
        if ([IO.Path]::GetDirectoryName($resolvedSession) -ne $baseRoot.TrimEnd("\") -or
            -not [IO.Path]::GetFileName($resolvedSession).StartsWith("deploom-vhdx-poc-")) {
            throw "UNSAFE_CLEANUP_TARGET: $resolvedSession"
        }
        try { Remove-Item -LiteralPath $resolvedSession -Recurse -Force -ErrorAction Stop }
        catch { Write-Warning "VHDX cleanup incomplete, retained at $session : $_" }
    }
}
if (-not $success) { throw 'VHDX_POC_INCOMPLETE: inspect report and retained artifacts if cleanup failed' }
