# Release version selection

The public push-github-branch-and-tag.ps1 wrapper delegates version selection to scripts/release-version.ps1 through push-branch-and-tag.ps1.

Without -Tag, VERSION determines the suggestion: 1.0.0-preview.2 becomes v1.0.0-preview.3; stable 1.0.0 becomes v1.0.1. A prerelease without a final numeric identifier gets .1 appended. Build metadata is dropped when incrementing.

An interactive terminal displays the suggestion and accepts Enter to use it, or a manually entered SemVer tag. An explicit -Tag takes precedence without prompting. CI, redirected input and -NonInteractive select the suggestion automatically. The public wrapper forwards -NonInteractive.

Versions use strict SemVer identifiers, including prerelease and build metadata. Existing local/remote tag guards and exact-commit validation remain mandatory. A partially published release must still be retried with the same explicit -Tag.
