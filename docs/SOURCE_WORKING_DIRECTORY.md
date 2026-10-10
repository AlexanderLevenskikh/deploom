# Working directory and migration inputs

A project has one `path`: the directory whose `package.json` is selected for dependency updates. `repositoryPath` records its Git repository root. Desktop validates that the two paths belong to the same repository; the repository root can be detected automatically when omitted. Selecting a root package never adds its child packages as migration targets. A directory without its own `package.json` is rejected instead of searching for a different package.

New iterative CLI runs use `--source-scope project-gitignore`. The isolated source contains Git-visible inputs of the selected package, declared package-manager workspace members and transitive `file:`, `link:` or `portal:` dependencies inside the repository. Workspace members are installation inputs, not automatically selected upgrade targets. Ancestor manifests, lockfiles, configuration and shared scripts remain available. Unrelated backend trees, root SQL/binary files, Git-ignored builds and caches are omitted. Tracked inputs are retained subject to the existing dependency/tool/editor exclusions. An explicitly configured local Yarn executable is retained separately from its cache. Git administrative data remains part of the isolated source.

The exact input policy is recorded in the snapshot manifest and included in the source identity. Reopen, verification materialization, cold-storage restore and index-only recovery preserve that policy. Scoped delivery leaves unselected tracked files at their original base versions and still guards their contents; an omitted directory is never interpreted as a requested deletion.

If validation depends on omitted inputs, it must fail rather than reuse an earlier green result. The explicit CLI escape hatch `--source-scope repository` retains the historical repository input policy. Existing runs without a stored scope keep their original policy; restarting creates a new run using the current default. No historical checkpoint is silently reinterpreted.

Yarn workspace paths beginning with `./` are normalized while preserving exclusions. Yarn 2/3/4 with node-modules uses its own immutable verification path (see MODERN_YARN.md); this does not authorize converting a Yarn project to npm. Its configured package manager and owning lockfile remain authoritative.

After verified delivery, the inspection checkout is detached at the verified commit, freeing the result branch for checkout elsewhere. Files and migration documents remain available. A changed result checkout is preserved. Optional documentation cleanup temporarily reacquires the branch and refuses to run if it is in use elsewhere.

Removing a workspace from Desktop removes only its registration. Project repositories, workspace files and migration results on disk remain intact. Active jobs or autopilot sessions prevent removal. Storage maintenance is a separate operation with its existing preservation checks.
