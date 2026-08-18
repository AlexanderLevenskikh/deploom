param(
    [string]$Root = "",
    [switch]$SeedYarn
)

$ErrorActionPreference = "Stop"

function Get-ReFsVolumes {
    Get-Volume |
        Where-Object { $_.FileSystem -eq "ReFS" -and $_.DriveLetter } |
        Sort-Object SizeRemaining -Descending
}

if (-not $Root) {
    $refs = @(Get-ReFsVolumes)
    if ($refs.Count -eq 0) {
        Write-Host "VEX_STORAGE_NO_REFS_VOLUME"
        Write-Host "Create a Windows Dev Drive/ReFS volume first, then rerun with -Root D:\DepLoom\verification"
        exit 2
    }
    $drive = $refs[0].DriveLetter
    $Root = "${drive}:\DepLoom\verification"
}

$rootPath = [System.IO.Path]::GetFullPath($Root)
$driveRoot = [System.IO.Path]::GetPathRoot($rootPath)
$driveLetter = $driveRoot.Substring(0, 1)
$volume = Get-Volume -DriveLetter $driveLetter

Write-Host "filesystem=$($volume.FileSystem)"
Write-Host "root=$rootPath"
try {
    fsutil devdrv query "${driveLetter}:" 2>$null
} catch {
    Write-Host "devdrive-query=unavailable-or-not-devdrive"
}

if ($volume.FileSystem -ne "ReFS") {
    Write-Host "VEX_STORAGE_NOT_REFS"
    Write-Host "The block is proof-safe on NTFS, but the Windows CoW acceleration will not activate."
    exit 3
}

New-Item -ItemType Directory -Force -Path $rootPath | Out-Null
[Environment]::SetEnvironmentVariable(
    "DEPLOOM_VERIFICATION_ROOT",
    $rootPath,
    [EnvironmentVariableTarget]::User
)
$env:DEPLOOM_VERIFICATION_ROOT = $rootPath

if ($SeedYarn) {
    $yarn = Get-Command yarn -ErrorAction SilentlyContinue
    if ($yarn) {
        $oldTarget = $env:YARN_CACHE_FOLDER
        try {
            Remove-Item Env:YARN_CACHE_FOLDER -ErrorAction SilentlyContinue
            $source = (& yarn cache dir).Trim()
        } finally {
            if ($oldTarget) {
                $env:YARN_CACHE_FOLDER = $oldTarget
            }
        }
        $target = Join-Path $rootPath "package-manager-artifacts\yarn"
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        if ($source -and (Test-Path $source)) {
            $hasFiles = Get-ChildItem -Force -ErrorAction SilentlyContinue $target | Select-Object -First 1
            if (-not $hasFiles) {
                Write-Host "Seeding Yarn cache: $source -> $target"
                & robocopy $source $target /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /SL /MT:16
                if ($LASTEXITCODE -ge 8) {
                    throw "Yarn cache seed robocopy failed: exit=$LASTEXITCODE"
                }
            } else {
                Write-Host "Yarn target cache already populated; seed skipped."
            }
        }
    } else {
        Write-Host "yarn=not-found; seed skipped"
    }
}

Write-Host "VEX_STORAGE_READY"
Write-Host "Restart DepLoom Desktop so it inherits the new user environment variable."
