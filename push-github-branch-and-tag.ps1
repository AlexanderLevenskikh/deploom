param(
    [Parameter(Mandatory = $true)]
    [string]$Commit,

    [string]$Tag = "",

    [string]$TagMessage = "",

    [switch]$SkipValidation
)

$ErrorActionPreference = "Stop"

$GitHubRepository = "AlexanderLevenskikh/deploom"
$ExpectedRemote = "git@github.com:AlexanderLevenskikh/deploom.git"
$RemoteName = "origin"
$RequiredBranch = "master"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseScript = Join-Path $ScriptRoot "push-branch-and-tag.ps1"

function Invoke-GitChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$ErrorMessage
    )

    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $ErrorMessage
    }
}

function Get-GitOutput {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$ErrorMessage
    )

    $Output = & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $ErrorMessage
    }
    return ($Output | Out-String).Trim()
}

Push-Location $ScriptRoot
try {
    if (-not (Test-Path $ReleaseScript)) {
        throw "Release helper not found: $ReleaseScript"
    }

    # The public repository intentionally starts from a sanitized snapshot with
    # fresh history.  Make the desired public branch explicit from the first
    # commit instead of inheriting the user's global init.defaultBranch.
    & git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "== Initialize fresh public Git repository =="
        & git init -b $RequiredBranch
        if ($LASTEXITCODE -ne 0) {
            Invoke-GitChecked -Arguments @('init') -ErrorMessage 'Failed to initialize Git repository.'
            Invoke-GitChecked -Arguments @('symbolic-ref', 'HEAD', "refs/heads/$RequiredBranch") -ErrorMessage "Failed to select initial branch '$RequiredBranch'."
        }
    }

    $RepoRoot = Get-GitOutput -Arguments @('rev-parse', '--show-toplevel') -ErrorMessage 'Cannot determine repository root.'
    $ResolvedRepoRoot = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\', '/')
    $ResolvedScriptRoot = [System.IO.Path]::GetFullPath($ScriptRoot).TrimEnd('\', '/')
    if ($ResolvedRepoRoot -ne $ResolvedScriptRoot) {
        throw "Run this script from the DepLoom repository root. Git root is '$ResolvedRepoRoot'."
    }

    & git rev-parse --verify HEAD *> $null
    $HasHead = ($LASTEXITCODE -eq 0)

    $Branch = (& git branch --show-current).Trim()
    if ([string]::IsNullOrWhiteSpace($Branch) -and -not $HasHead) {
        Invoke-GitChecked -Arguments @('symbolic-ref', 'HEAD', "refs/heads/$RequiredBranch") -ErrorMessage "Failed to select initial branch '$RequiredBranch'."
        $Branch = $RequiredBranch
    }

    if ($Branch -ne $RequiredBranch) {
        throw "Public DepLoom releases must be made from '$RequiredBranch', current branch is '$Branch'."
    }

    # Enforce SSH transport to one exact public repository.  Converting the
    # same GitHub repository from HTTPS to SSH is safe; any unrelated origin is
    # treated as a hard stop rather than silently redirected.
    $CurrentRemote = & git remote get-url $RemoteName 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "== Add GitHub SSH remote =="
        Invoke-GitChecked -Arguments @('remote', 'add', $RemoteName, $ExpectedRemote) -ErrorMessage "Failed to add '$RemoteName' remote."
    }
    else {
        $CurrentRemote = ($CurrentRemote | Out-String).Trim()
        $SameRepoHttps = $CurrentRemote -match '^https://github\.com/AlexanderLevenskikh/deploom(?:\.git)?/?$'
        if ($CurrentRemote -eq $ExpectedRemote) {
            # Already correct.
        }
        elseif ($SameRepoHttps) {
            Write-Host "== Convert origin from HTTPS to SSH =="
            Invoke-GitChecked -Arguments @('remote', 'set-url', $RemoteName, $ExpectedRemote) -ErrorMessage "Failed to switch '$RemoteName' to SSH."
        }
        else {
            throw "Remote '$RemoteName' points to '$CurrentRemote'. Refusing to publish DepLoom to anything except '$ExpectedRemote'."
        }
    }

    $VerifiedRemote = Get-GitOutput -Arguments @('remote', 'get-url', $RemoteName) -ErrorMessage "Cannot read '$RemoteName' remote."
    if ($VerifiedRemote -ne $ExpectedRemote) {
        throw "GitHub SSH remote mismatch: expected '$ExpectedRemote', got '$VerifiedRemote'."
    }

    Write-Host ""
    Write-Host "========================================"
    Write-Host "DepLoom GitHub release"
    Write-Host "========================================"
    Write-Host "Repository: $GitHubRepository"
    Write-Host "Remote:     $ExpectedRemote"
    Write-Host "Branch:     $RequiredBranch"

    Write-Host ""
    Write-Host "== Verify GitHub SSH access =="
    & git ls-remote $RemoteName *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot access '$ExpectedRemote' over SSH. Check your GitHub SSH key with: ssh -T git@github.com"
    }

    $ReleaseArgs = @{
        Commit = $Commit
        Remote = $RemoteName
    }
    if (-not [string]::IsNullOrWhiteSpace($Tag)) {
        $ReleaseArgs.Tag = $Tag
    }
    if (-not [string]::IsNullOrWhiteSpace($TagMessage)) {
        $ReleaseArgs.TagMessage = $TagMessage
    }
    if ($SkipValidation) {
        $ReleaseArgs.SkipValidation = $true
    }

    & $ReleaseScript @ReleaseArgs
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub release helper failed."
    }

    $FinalRemote = Get-GitOutput -Arguments @('remote', 'get-url', $RemoteName) -ErrorMessage "Cannot verify final '$RemoteName' remote."
    if ($FinalRemote -ne $ExpectedRemote) {
        throw "Release completed but origin drifted from the required SSH URL."
    }

    # A Git push controls refs but not GitHub repository metadata.  On an empty
    # repository GitHub normally adopts the first branch, but we verify the
    # server-side HEAD and, when GitHub CLI is available, correct it explicitly.
    $RemoteHead = (& git ls-remote --symref $RemoteName HEAD 2>$null | Out-String)
    $MasterHead = "ref: refs/heads/$RequiredBranch"
    if ($RemoteHead -notmatch [regex]::Escape($MasterHead)) {
        $Gh = Get-Command gh -ErrorAction SilentlyContinue
        if ($Gh) {
            Write-Host ""
            Write-Host "== Set GitHub default branch to master =="
            & gh repo edit $GitHubRepository --default-branch $RequiredBranch
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Branch/tag were published, but GitHub default branch could not be changed automatically. Run: gh repo edit $GitHubRepository --default-branch $RequiredBranch"
            }
        }
        else {
            Write-Warning "Branch/tag were published to master, but GitHub still reports another default branch and GitHub CLI is not installed. Set Settings -> Default branch to 'master', or run: gh repo edit $GitHubRepository --default-branch $RequiredBranch"
        }
    }

    Write-Host ""
    Write-Host "GitHub publication uses SSH and branch '$RequiredBranch'."
}
finally {
    Pop-Location
}
