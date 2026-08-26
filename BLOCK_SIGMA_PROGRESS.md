# Block Sigma progress

## Workstream 1 — reparse-safe materialization

Status: completed.

- Added a single recursive reparse inventory and reconstruction plan.
- Guarded clones now create private scoped shells and rebase owned workspace links into the private upper.
- Private snapshot copies exclude junction traversal and reconstruct only validated in-root links.
- External, broken, undeclared, and cyclic reparse targets fail closed to a private fallback.
- Added Windows physical acceptance coverage for scoped, nested, sibling, external, and cyclic links.

Focused validation:

- `python -m unittest tests.test_prepared_workspace_fastpath tests.test_block_z_project_topology tests.test_snapshot_copy_progress_hotfix tests.acceptance.test_block_z_workspace_physical tests.acceptance.test_block_omega_guarded_lower_physical` — 28 tests passed.
- `python -m unittest tests.acceptance.test_block_omega_guarded_lower_physical tests.test_block_z_project_topology tests.test_snapshot_copy_progress_hotfix` — 28 tests passed after the new physical cases.
- `git diff --check` — passed.

## Workstream 2 — sealed SourceSnapshot evidence and live-checkout baseline

Status: completed.

- Compatibility evidence schema v2 carries an exact durable SourceSnapshot locator, key, and project-relative path.
- Evidence reproduction validates and copies the sealed tree; Git branch cloning is no longer an authority path.
- Root/nested absolute gitdir, commondir, and object alternates that can escape the sealed tree fail closed.
- Fresh baseline accepts dirty, untracked, ignored, and detached live checkouts without stash or branch switching.
- Post-baseline cleanup preserves the current checkout and active branch while retiring the prior execution epoch.

Focused validation:

- python -m unittest tests.test_block_x_source_truth tests.test_dependency_compatibility_evidence tests.regression.test_project_isolation — 32 tests passed, 1 platform capability skip.
- node desktop/scripts/check-autopilot.mjs — passed.
- npm exec tsc -- --noEmit -p tsconfig.electron.json — passed.
- git diff --check — passed.

## Workstream 3 — durable integrity and tool build identity

Status: completed.

- PreparedArtifact index schema v2 stores a strong whole-tree content key, counts, reparse semantics, and toolBuildId.
- Publication hashes every regular file and validates directory stability; the first HIT in a fresh process revalidates the complete tree.
- Same-size tampering with restored mtime invalidates the locator and falls back safely.
- toolBuildId is included in SourceSnapshot, resolver/preparation/project proof identity, trial keys, learned-constraint cache, durable artifacts, and ProofEnvelope schema v5.
- Legacy artifact/cache/envelope schemas and stale tool builds are safe misses.

Focused validation:

- PreparedArtifact/GC tests — 10 passed.
- Proof/cache/proven-state tests — 53 passed.
- SourceSnapshot/evidence/artifact combined gate — 31 passed, 1 platform capability skip.
- Final toolBuildId/evidence/proof gate — 34 passed.
- Electron TypeScript noEmit, migration-progress contract, and autopilot contract — passed.
- git diff --check — passed.
