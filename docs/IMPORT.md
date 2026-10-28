# Import the staged headless Core into DepLoom/core

The source tree is staged on
`AlexanderLevenskikh/deploom:extract/headless-core-bootstrap-20261028`.
It contains only the 133-file headless runtime inventory, focused tests,
standalone Python packaging and a dedicated lightweight CI. The source
repository's `master` is deliberately unchanged.

## One required GitHub UI action

Create **an empty** GitHub repository named `DepLoom/core`, with no initial
README, license, or .gitignore. This is a repository-provisioning step;
the currently connected GitHub integration cannot create repositories.

## Import without monorepo Git history (PowerShell)

Run in a separate temporary directory, NOT inside an existing product checkout:

```powershell
git clone --depth 1 --single-branch --branch extract/headless-core-bootstrap-20261028 https://github.com/AlexanderLevenskikh/deploom.git core-bootstrap
Set-Location core-bootstrap
git log -1 --oneline
git checkout --orphan main
git add -A
git commit -m "bootstrap: headless DepLoom Core"
git remote set-url origin https://github.com/DepLoom/core.git
git push -u origin main
```

The new root commit intentionally has a **different SHA** from the staging
branch but the same source file bytes. Keep the pushed SHA and a tree inventory
receipt before updating consumers. The import script does not publish a
package release or alter the historical repository.

## Required follow-up in DepLoom/mcp

1. Change `.gitmodules` from the old repository to `DepLoom/core` and
   update the pinned `upstream/deploom` submodule commit to the new repo.
2. Regenerate `release-manifest.json` from the new source files
   (`peer_solver_transition.py` and Windows path normalization changed);
   do not reuse old SHA-256 values.
3. Run `scripts/update_release_manifest.py --check`,
   `scripts/check_skill.py`, focused target/decision tests, stdio smoke and
   the isolated installed-wheel smoke in MCP.
4. Run one real-Core **planning-only** synthetic `required` invocation
   through MCP and verify `DRAFT_READY` or a truthful solver blocker, not
   `INVALID_CURRENT_ENDPOINT`.
5. Merge the MCP re-pin only after its CI passes; only then freeze its exact
   Core+MCP identities for the no-Skill experiment.

Do not label the current MCP main as using this extracted Core: it still
pins the historical Core SHA until these steps are done.
