param(
    [Parameter(Mandatory = $true)]
    [string]$Commit,

    [string]$Tag = "",

    [string]$Remote = "origin",

    [string]$TagMessage = "",

    [switch]$SkipValidation
)

$ErrorActionPreference = "Stop"

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,

        [Parameter(Mandatory = $true)]
        [string]$ErrorMessage
    )

    & $Command

    if ($LASTEXITCODE -ne 0) {
        throw $ErrorMessage
    }
}

function Remove-ValidationWorktree {
    param(
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }

    if (Test-Path $Path) {
        Write-Host ""
        Write-Host "== Remove validation worktree =="

        & git worktree remove --force $Path

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Failed to remove validation worktree '$Path'."
        }
    }

    & git worktree prune *> $null
}

# ------------------------------------------------------------
# Repository
# ------------------------------------------------------------

& git rev-parse --is-inside-work-tree *> $null

if ($LASTEXITCODE -ne 0) {
    throw "Not inside a Git repository."
}

$RepoRoot = (& git rev-parse --show-toplevel).Trim()

if (
    $LASTEXITCODE -ne 0 -or
    [string]::IsNullOrWhiteSpace($RepoRoot)
) {
    throw "Cannot determine repository root."
}

Push-Location $RepoRoot

$ValidationWorktree = $null
$VersionCommitCreated = $false
$BranchPublished = $false

try {
    # --------------------------------------------------------
    # Branch
    # --------------------------------------------------------

    $Branch = (& git branch --show-current).Trim()

    if (
        $LASTEXITCODE -ne 0 -or
        [string]::IsNullOrWhiteSpace($Branch)
    ) {
        throw "Cannot determine current branch. Detached HEAD?"
    }

    # --------------------------------------------------------
    # Repository state
    # --------------------------------------------------------

    & git rev-parse --verify HEAD *> $null
    $HasHead = ($LASTEXITCODE -eq 0)

    # --------------------------------------------------------
    # Remote
    # --------------------------------------------------------

    & git remote get-url $Remote *> $null

    if ($LASTEXITCODE -ne 0) {
        throw "Git remote '$Remote' does not exist."
    }

    # --------------------------------------------------------
    # Paths
    # --------------------------------------------------------

    $DesktopPath = Join-Path $RepoRoot "desktop"
    $PackageJsonPath = Join-Path $DesktopPath "package.json"
    $PackageLockPath = Join-Path $DesktopPath "package-lock.json"
    $VersionPath = Join-Path $RepoRoot "VERSION"

    if (-not (Test-Path $PackageJsonPath)) {
        throw "package.json not found: $PackageJsonPath"
    }

    if (-not (Test-Path $PackageLockPath)) {
        throw "package-lock.json not found: $PackageLockPath"
    }

    if (-not (Test-Path $VersionPath)) {
        throw "VERSION not found: $VersionPath"
    }

    # --------------------------------------------------------
    # Resolve version
    # --------------------------------------------------------

    if ([string]::IsNullOrWhiteSpace($Tag)) {
        $CurrentVersion = (Get-Content $VersionPath -Raw).Trim()

        if ($CurrentVersion -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
            throw (
                "Cannot automatically increment VERSION " +
                "'$CurrentVersion'. Expected MAJOR.MINOR.PATCH."
            )
        }

        $Major = [int]$Matches[1]
        $Minor = [int]$Matches[2]
        $Patch = [int]$Matches[3] + 1

        $Version = "$Major.$Minor.$Patch"
        $Tag = "v$Version"

        Write-Host ""
        Write-Host "Automatic patch bump:"
        Write-Host "  $CurrentVersion -> $Version"
    }
    else {
        $Version = $Tag

        if ($Version.StartsWith("v")) {
            $Version = $Version.Substring(1)
        }

        if (
            $Version -notmatch
            '^\d+\.\d+\.\d+([-.+][0-9A-Za-z.-]+)?$'
        ) {
            throw (
                "Unsupported version '$Version'. " +
                "Expected something like 0.1.90."
            )
        }

        $Tag = "v$Version"
    }

    Write-Host ""
    Write-Host "========================================"
    Write-Host "Release preparation"
    Write-Host "========================================"
    Write-Host "Branch:  $Branch"
    Write-Host "Version: $Version"
    Write-Host "Tag:     $Tag"
    Write-Host "Remote:  $Remote"

    # --------------------------------------------------------
    # Check tag BEFORE making commits
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "== Check tag =="

    $RemoteTag = & git ls-remote --tags `
        $Remote `
        "refs/tags/$Tag"

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to check remote tag '$Tag'."
    }

    if ($RemoteTag) {
        throw "Tag '$Tag' already exists on remote '$Remote'."
    }

    & git show-ref --verify --quiet "refs/tags/$Tag"

    if ($LASTEXITCODE -eq 0) {
        throw "Tag '$Tag' already exists locally."
    }

    # --------------------------------------------------------
    # COMMIT 1
    # Commit current source changes
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "== Commit current changes =="

    $WorkspaceNoisePathspecs = @(
        ':(glob,exclude)**/.idea/**',
        ':(glob,exclude)**/.vs/**',
        ':(glob,exclude)**/.vscode/**',
        ':(glob,exclude)**/.fleet/**',
        ':(glob,exclude)**/.history/**',
        ':(glob,exclude)**/*.swp',
        ':(glob,exclude)**/*.swo',
        ':(glob,exclude)**/*.suo',
        ':(glob,exclude)**/*.user',
        ':(glob,exclude)**/*.userosscache',
        ':(glob,exclude)**/*.sln.docstates',
        ':(glob,exclude)**/.DS_Store',
        ':(glob,exclude)**/Thumbs.db',
        ':(glob,exclude)**/desktop.ini',
        ':(glob,exclude)**/*~'
    )

    & git add -A -- . @WorkspaceNoisePathspecs

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stage current changes."
    }

    # VERSION files normally belong to the second commit.  An unborn
    # repository has no HEAD to reset against, so the first public source
    # commit intentionally contains the already-selected version files.
    if ($HasHead) {
        & git reset -- `
            VERSION `
            desktop/package.json `
            desktop/package-lock.json

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to exclude version files from source commit."
        }
    }

    & git diff --cached --quiet
    $HasSourceChanges = ($LASTEXITCODE -ne 0)

    if ($HasSourceChanges) {
        Write-Host ""
        Write-Host "Creating source commit:"
        Write-Host "  $Commit"
        Write-Host ""

        & git diff --cached --stat

        & git commit -m $Commit

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create source commit."
        }
    }
    else {
        Write-Host "No source changes to commit."
    }

    # --------------------------------------------------------
    # Ensure version files do not contain unrelated changes
    # --------------------------------------------------------

    $DirtyVersionFiles = @(
        & git status --porcelain -- `
            VERSION `
            desktop/package.json `
            desktop/package-lock.json
    )

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to inspect version files."
    }

    if ($DirtyVersionFiles.Count -gt 0) {
        Write-Host ""
        Write-Host "Version files already contain local changes:"

        $DirtyVersionFiles | ForEach-Object {
            Write-Host "  $_"
        }

        throw (
            "VERSION/package files must be clean before " +
            "automatic version bump."
        )
    }

    # --------------------------------------------------------
    # Update version
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "== Update version =="

    $CurrentPackageVersion = (
        & node -p "require('./desktop/package.json').version"
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read desktop/package.json version."
    }

    $CurrentPackageLockVersion = (
        & node -p "require('./desktop/package-lock.json').version"
    ).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read desktop/package-lock.json version."
    }

    $CurrentVersionFileValue = (Get-Content $VersionPath -Raw).Trim()
    $VersionAlreadyCurrent = (
        $CurrentPackageVersion -eq $Version -and
        $CurrentPackageLockVersion -eq $Version -and
        $CurrentVersionFileValue -eq $Version
    )

    if ($VersionAlreadyCurrent) {
        Write-Host "Version files already match $Version; no version commit is required."
    }
    else {
        Push-Location $DesktopPath

        try {
            & npm version $Version `
                --no-git-tag-version `
                --allow-same-version `
                --ignore-scripts

            if ($LASTEXITCODE -ne 0) {
                throw (
                    "Failed to update desktop/package.json " +
                    "and desktop/package-lock.json."
                )
            }
        }
        finally {
            Pop-Location
        }

        # Root VERSION without BOM.
        $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText(
            $VersionPath,
            "$Version`n",
            $Utf8NoBom
        )
    }

    # --------------------------------------------------------
    # Verify versions before commit
    # --------------------------------------------------------

    $PackageVersion = (
        & node -p `
            "require('./desktop/package.json').version"
    ).Trim()

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read desktop/package.json version."
    }

    $PackageLockVersion = (
        & node -p `
            "require('./desktop/package-lock.json').version"
    ).Trim()

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read desktop/package-lock.json version."
    }

    $VersionFileValue = (
        Get-Content $VersionPath -Raw
    ).Trim()

    if ($PackageVersion -ne $Version) {
        throw (
            "desktop/package.json: expected '$Version', " +
            "got '$PackageVersion'."
        )
    }

    if ($PackageLockVersion -ne $Version) {
        throw (
            "desktop/package-lock.json: expected '$Version', " +
            "got '$PackageLockVersion'."
        )
    }

    if ($VersionFileValue -ne $Version) {
        throw (
            "VERSION: expected '$Version', " +
            "got '$VersionFileValue'."
        )
    }

    if ($Tag -ne "v$Version") {
        throw (
            "Tag/version mismatch: tag '$Tag', " +
            "expected 'v$Version'."
        )
    }

    Write-Host ""
    Write-Host "Versions OK:"
    Write-Host "  VERSION                   $VersionFileValue"
    Write-Host "  desktop/package.json      $PackageVersion"
    Write-Host "  desktop/package-lock.json $PackageLockVersion"
    Write-Host "  tag                       $Tag"

    # --------------------------------------------------------
    # COMMIT 2
    # Version-only commit
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "== Commit version =="

    if (-not $VersionAlreadyCurrent) {
        & git add -- `
            VERSION `
            desktop/package.json `
            desktop/package-lock.json

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to stage version files."
        }

        # Guarantee that ONLY version files are staged.
        $StagedFiles = @(
            & git diff --cached --name-only
        )

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to inspect staged version changes."
        }

        $AllowedVersionFiles = @(
            "VERSION",
            "desktop/package.json",
            "desktop/package-lock.json"
        )

        $UnexpectedStagedFiles = @(
            $StagedFiles | Where-Object {
                $_ -notin $AllowedVersionFiles
            }
        )

        if ($UnexpectedStagedFiles.Count -gt 0) {
            Write-Host ""
            Write-Host "Unexpected files staged for release commit:"

            $UnexpectedStagedFiles | ForEach-Object {
                Write-Host "  $_"
            }

            throw "Release commit contains non-version files."
        }

        & git diff --cached --quiet
        $HasVersionChanges = ($LASTEXITCODE -ne 0)

        if (-not $HasVersionChanges) {
            throw "Version update unexpectedly produced no changes."
        }

        & git commit -m "chore: release $Tag"

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create version commit."
        }

        $VersionCommitCreated = $true
    }
    else {
        Write-Host "No version commit needed for $Tag."
    }

    $ReleaseCommit = (& git rev-parse HEAD).Trim()

    Write-Host ""
    Write-Host "Release commit:"
    Write-Host "  $ReleaseCommit"

    # --------------------------------------------------------
    # Worktree must now be clean
    # --------------------------------------------------------

    $RemainingChanges = @(
        & git status --porcelain | Where-Object {
            $_ -notmatch '(^|[\/])\.(idea|vs|vscode|fleet|history)([\/]|$)' -and
            $_ -notmatch '(\.swp|\.swo|\.suo|\.user|\.userosscache|\.sln\.docstates|~)$' -and
            $_ -notmatch '([\/]|^)(\.DS_Store|Thumbs\.db|desktop\.ini)$'
        }
    )

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to inspect final worktree."
    }

    if ($RemainingChanges.Count -gt 0) {
        Write-Host ""
        Write-Host "Uncommitted changes remain:"

        $RemainingChanges | ForEach-Object {
            Write-Host "  $_"
        }

        throw "Unexpected changes remain before validation."
    }

    # --------------------------------------------------------
    # Validate EXACT release commit in clean worktree
    # --------------------------------------------------------

    if (-not $SkipValidation) {
        Write-Host ""
        Write-Host "========================================"
        Write-Host "Validate release commit"
        Write-Host "========================================"

        $ValidationWorktree = Join-Path `
            ([System.IO.Path]::GetTempPath()) `
            "dependency-roadmap-release-$PID-$Version"

        if (Test-Path $ValidationWorktree) {
            Remove-Item `
                -Recurse `
                -Force `
                $ValidationWorktree
        }

        Write-Host ""
        Write-Host "== Create clean validation worktree =="
        Write-Host $ValidationWorktree

        & git worktree add `
            --detach `
            $ValidationWorktree `
            $ReleaseCommit

        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create validation worktree."
        }

        try {
            Push-Location $ValidationWorktree

            try {
                Write-Host ""
                Write-Host "== Validation environment =="

                Write-Host -NoNewline "Node: "
                & node --version

                if ($LASTEXITCODE -ne 0) {
                    throw "Node.js is not available."
                }

                Write-Host -NoNewline "npm:  "
                & npm --version

                if ($LASTEXITCODE -ne 0) {
                    throw "npm is not available."
                }

                # --------------------------------------------
                # Root checks used by CI
                # --------------------------------------------

                if (
                    Test-Path `
                        "scripts/check-public-sanitization.py"
                ) {
                    Write-Host ""
                    Write-Host "== check-public-sanitization =="

                    & python `
                        scripts/check-public-sanitization.py

                    if ($LASTEXITCODE -ne 0) {
                        throw "check-public-sanitization failed."
                    }
                }

                if (
                    Test-Path `
                        "scripts/check-release-assets.mjs"
                ) {
                    Write-Host ""
                    Write-Host "== check-release-assets =="

                    & node `
                        scripts/check-release-assets.mjs

                    if ($LASTEXITCODE -ne 0) {
                        throw "check-release-assets failed."
                    }
                }

                if (
                    Test-Path `
                        "scripts/check-package-windows-resilience.mjs"
                ) {
                    Write-Host ""
                    Write-Host "== check-package-windows-resilience =="

                    & node `
                        scripts/check-package-windows-resilience.mjs

                    if ($LASTEXITCODE -ne 0) {
                        throw (
                            "check-package-windows-resilience failed."
                        )
                    }
                }

                # --------------------------------------------
                # Desktop validation
                # --------------------------------------------

                Push-Location "desktop"

                try {
                    Write-Host ""
                    Write-Host "== npm ci =="

                    & npm ci

                    if ($LASTEXITCODE -ne 0) {
                        throw "npm ci failed."
                    }

                    Write-Host ""
                    Write-Host "== npm run lint =="

                    & npm run lint

                    if ($LASTEXITCODE -ne 0) {
                        throw "npm run lint failed."
                    }

                    Write-Host ""
                    Write-Host "== npm run build =="

                    & npm run build

                    if ($LASTEXITCODE -ne 0) {
                        throw "npm run build failed."
                    }

                    # ----------------------------------------
                    # Automatically run every check:* script
                    #
                    # This means newly-added contract checks
                    # automatically become release gates.
                    # ----------------------------------------

                    Write-Host ""
                    Write-Host "== Discover check:* scripts =="

                    $DesktopPackageJsonPath = Join-Path (Get-Location) "package.json"

                    if (-not (Test-Path $DesktopPackageJsonPath)) {
                        throw "package.json not found in validation desktop directory: $DesktopPackageJsonPath"
                    }

                    try {
                        $DesktopPackageJson = Get-Content `
                            -LiteralPath $DesktopPackageJsonPath `
                            -Raw |
                            ConvertFrom-Json
                    }
                    catch {
                        throw "Failed to parse package.json: $($_.Exception.Message)"
                    }

                    $CheckScripts = @(
                        $DesktopPackageJson.scripts.PSObject.Properties |
                            Where-Object { $_.Name -like "check:*" } |
                            ForEach-Object { $_.Name }
                    )

                    Write-Host "Found $($CheckScripts.Count) check script(s)."

                    if ($CheckScripts.Count -eq 0) {
                        Write-Host "No check:* scripts found."
                    }
                    else {
                        foreach ($CheckScript in $CheckScripts) {
                            Write-Host ""
                            Write-Host "== npm run $CheckScript =="

                            & npm run $CheckScript

                            if ($LASTEXITCODE -ne 0) {
                                throw "Release validation failed: npm run $CheckScript"
                            }
                        }
                    }
                }
                finally {
                    Pop-Location
                }

                Write-Host ""
                Write-Host "========================================"
                Write-Host "LOCAL RELEASE VALIDATION PASSED"
                Write-Host "========================================"
            }
            finally {
                Pop-Location
            }
        }
        finally {
            Remove-ValidationWorktree `
                -Path $ValidationWorktree

            $ValidationWorktree = $null
        }
    }
    else {
        Write-Host ""
        Write-Host "WARNING: release validation skipped."
    }

    # --------------------------------------------------------
    # Push only AFTER validation
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "== Push branch =="

    & git push -u $Remote $Branch

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to push branch '$Branch'."
    }

    # The release commit is externally visible now. A later tag failure must
    # never rewrite local history away from the published branch.
    $BranchPublished = $true

    # --------------------------------------------------------
    # Create tag
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "== Create tag =="

    if ([string]::IsNullOrWhiteSpace($TagMessage)) {
        $TagMessage = "Release $Tag"
    }

    & git tag -a $Tag -m $TagMessage

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create tag '$Tag'."
    }

    # --------------------------------------------------------
    # Push tag
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "== Push tag =="

    & git push $Remote "refs/tags/$Tag"

    if ($LASTEXITCODE -ne 0) {
        # Tag exists only locally if this fails.
        throw "Failed to push tag '$Tag'."
    }

    # --------------------------------------------------------
    # Done
    # --------------------------------------------------------

    Write-Host ""
    Write-Host "========================================"
    Write-Host "RELEASE COMPLETED"
    Write-Host "========================================"
    Write-Host "Branch:  $Branch"
    Write-Host "Commit:  $ReleaseCommit"
    Write-Host "Version: $Version"
    Write-Host "Tag:     $Tag"
    Write-Host "Remote:  $Remote"

    # Success: do NOT execute rollback.
    $VersionCommitCreated = $false
}
catch {
    $OriginalError = $_

    # Clean temporary worktree first.

    if ($ValidationWorktree) {
        Remove-ValidationWorktree `
            -Path $ValidationWorktree

        $ValidationWorktree = $null
    }

    # --------------------------------------------------------
    # If validation failed after version commit but before
    # publication, undo ONLY the version commit.
    #
    # Source commit remains intact.
    # VERSION returns to previous value, so rerunning the
    # script produces the SAME next patch version.
    # --------------------------------------------------------

    if ($VersionCommitCreated -and -not $BranchPublished) {
        Write-Host ""
        Write-Host "== Roll back local version commit =="

        $CurrentHeadMessage = (
            & git log -1 --pretty=%s
        ).Trim()

        if (
            $LASTEXITCODE -eq 0 -and
            $CurrentHeadMessage -eq "chore: release $Tag"
        ) {
            & git reset --hard HEAD^

            if ($LASTEXITCODE -eq 0) {
                Write-Host (
                    "Rolled back failed local release $Tag."
                )

                Write-Host (
                    "Your source commit was preserved."
                )
            }
            else {
                Write-Warning (
                    "Failed to roll back version commit."
                )
            }
        }
        else {
            Write-Warning (
                "HEAD changed unexpectedly; " +
                "automatic version rollback skipped."
            )
        }
    }

    throw $OriginalError
}
finally {
    Pop-Location
}
