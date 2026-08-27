# Block Sigma progress

Implementation status: **complete; authoritative final Windows gate pending**.

This file is a resumable engineering checkpoint only. It is not runtime/proof authority.

## Workstream 1 вЂ” reparse-safe materialization

Status: completed.

- Added a single recursive reparse inventory and reconstruction plan.
- Guarded clones create private scoped shells and rebase owned workspace links into the private upper.
- Private snapshot copies exclude junction traversal and reconstruct only validated in-root links.
- External, broken, undeclared, and cyclic reparse targets fail closed to a private fallback.
- Added Windows physical acceptance coverage for scoped, nested, sibling, external, and cyclic links.

Codex focused validation before handoff:

- `python -m unittest tests.test_prepared_workspace_fastpath tests.test_block_z_project_topology tests.test_snapshot_copy_progress_hotfix tests.acceptance.test_block_z_workspace_physical tests.acceptance.test_block_omega_guarded_lower_physical` вЂ” PASS.

## Workstream 2 вЂ” sealed SourceSnapshot evidence and live-checkout baseline

Status: completed.

- Compatibility evidence schema v2 carries the exact durable SourceSnapshot locator, key, and project-relative path.
- Evidence reproduction validates and copies the sealed tree; Git branch cloning is no longer an authority path.
- Root/nested absolute gitdir, commondir, and object alternates that escape the sealed tree fail closed.
- Fresh baseline accepts dirty, untracked, ignored, and detached live checkouts without stash/branch switching.
- Post-baseline cleanup preserves the current checkout/branch while retiring the previous execution epoch.

Codex focused validation before handoff:

- Source Truth/evidence/project-isolation tests вЂ” PASS, with platform capability skips only.
- Electron TypeScript noEmit/migration/autopilot contracts вЂ” PASS.

## Workstream 3 вЂ” durable integrity and tool build identity

Status: completed.

- PreparedArtifact index schema v2 stores a strong whole-tree content key, counts, reparse semantics, and toolBuildId.
- Publication hashes all regular files and validates directory stability; first HIT in a fresh process revalidates the complete tree.
- Same-size tampering with restored mtime invalidates the locator and falls back safely.
- toolBuildId participates in SourceSnapshot, resolver/preparation/project proof identity, trial keys, learned-constraint cache, durable artifacts, and ProofEnvelope schema v5.
- Legacy artifact/cache/envelope schemas and stale tool builds are safe misses.

Codex focused validation before handoff:

- PreparedArtifact/GC, proof/cache/proven-state, SourceSnapshot/evidence/artifact gates вЂ” PASS.

## Workstream 4 вЂ” context-safe learned constraints

Status: completed.

- Persistent learning no longer promotes minimized/context-local witnesses into universal solver authority.
- The durable authoritative fallback is an exact complete solver-managed assignment exclusion.
- Exact exclusions are coordinated across otherwise-independent solver components instead of merging component topology.
- Persistent constraint schema is `verified-resolver-exact-assignment-v4-context-safe`; stale context-unsafe cache entries are ignored.
- Telemetry identifies exact-assignment scope and `universal=false`.
- Diagnostic graph minimization remains useful for navigation/certification but does not become universal authority by itself.

Safety invariant:

`FAIL(A1,B2,C1)` may exclude that exact certified assignment; it may not silently become universal `NOT(A1 AND B2)`.

## Workstream 5 вЂ” descendant supervision and bounded subprocess output

Status: completed.

- Windows verification requires successful Job Object attachment; supervision uncertainty is infrastructure, not a guarded PASS.
- Successful commands also pass through descendant-tree quiescence before watcher/lease cleanup.
- Linux enables child-subreaper supervision and kills token-owned descendants, including a child that detached with `setsid()`.
- Other POSIX remains typed best-effort; shared/reuse-sensitive paths require `guaranteed-tree`.
- Subprocess output is bounded head+tail storage while a streaming observer retains full-stream infrastructure classification.
- Telemetry records captured/dropped bytes, truncation, supervision quality, descendants killed/remaining, and quiescence time.

Focused validation:

- `tests.test_sigma_process_supervision` is included in the aggregate focused gate below.

## Workstream 6 вЂ” physical acceptance and CI gate

Status: completed.

- `run_tool_tests.py` now has `unit`, `regression`, `acceptance`, and `all`; `all` includes all three suites.
- Windows CI installs Yarn Classic and runs the verification substrate tests plus the full physical acceptance suite.
- Added/extended physical cases for scoped Yarn Classic workspace rebasing and descendant-process supervision.
- Added a regression contract ensuring the documented package-manager support matrix matches code policy.

Local Linux acceptance on 2026-08-27:

- `python run_tool_tests.py --suite acceptance` вЂ” **12 tests, PASS, 7 platform skips**.
- The skips are Windows-only physical cases and are intentionally NOT counted as Windows PASS.

## Workstream 7 вЂ” SourceSnapshot / I/O performance closure

Status: completed.

- Source capture now hashes the sealed snapshot and one final live-source state instead of pre + sealed + post full byte manifests.
- `SourceSnapshotKey` is still derived from the sealed manifest; live-final mismatch fails/retries capture.
- Source-manifest hashing uses bounded parallel workers and bounded pending futures rather than allocating one future per file.
- PreparedArtifact whole-tree integrity hashing is similarly bounded.
- Heavy filesystem operations use the proof-neutral process-wide I/O governor (`copy`, `hash`, `pm` slots).
- Robocopy parallelism is bounded more conservatively to avoid per-trial thread storms.
- Performance knobs do not participate in proof identity.

Remaining performance evidence requirement:

- Measure a fresh production-like `representative production-like project` run after the authoritative Windows gate. No production wall-clock improvement is claimed from the Linux fixture alone.

## Workstream 8 вЂ” release hardening and real-run repair

Status: completed.

- Project-check infrastructure classification observes the full subprocess stream; the 80-line tail remains presentation only.
- Installed-package observation cannot walk above the authoritative PackageManagerRoot/trial boundary.
- Ephemeral cache cleanup follows canonical dependency roots.
- POSIX project checks use non-login `sh -c` semantics.
- README now states the actual authoritative package-manager matrix: npm + Yarn Classic supported; Yarn Berry/PnP/pnpm typed unsupported/fail-closed.
- Added `tests/regression/test_package_manager_support_matrix.py`.

### Real `representative production-like project` bug discovered during Sigma

Fixed `PREPARED_SNAPSHOT_CLONE_FAILED ... WORKSPACE_TARGET_EXISTS: .../project-check-transaction`.

Root cause:

- a reusable project-check trial could be invalidated after a mutating command such as `yarn build`;
- Windows/ReFS/filter-driver handle lifetime could defer `shutil.rmtree`;
- the next command reused the same fixed pathname, so strict materialization correctly rejected the still-existing target.

Fix:

- every fresh command gets a monotonic namespaced target (`project-check-001`, `project-check-002`, ...);
- an unexpected occupied namespace gets a fresh sibling suffix instead of merge/overwrite;
- cleanup has bounded retry/backoff and emits `verify.workspace.cleanup-deferred` rather than poisoning the next verification command;
- strict `WORKSPACE_TARGET_EXISTS` isolation remains intact.

Regression coverage is in `tests.test_sigma_process_supervision`.

## Workstream 9 вЂ” final gate and repair loop

Status: implementation-complete; Windows authority pending.

Validation performed in the available Linux environment:

1. `python -m compileall -q .` вЂ” PASS.
2. Aggregate Sigma/source/substrate focused gate вЂ” **152 tests PASS, 12 platform skips**.
3. `python run_tool_tests.py --suite acceptance` вЂ” **12 tests PASS, 7 Windows-only skips**.
4. `python -m unittest tests.regression.test_package_manager_support_matrix` вЂ” **2 tests PASS**.
5. Broad 484-test non-curated run reached only expected environment-dependent failures:
   - exact-solver tests fail because this environment has no `z3-solver` and internet is disabled;
   - one CLI physical fixture fails because Yarn is not installed here.
   These are NOT recorded as PASS and must be rerun on Windows with the project `.venv`/Yarn installation.
6. Desktop build was not run here because `desktop/node_modules` is absent and network installation is unavailable.

### Required authoritative Windows gate

Run from the repository root after applying this checkpoint:

```powershell
.\.venv\Scripts\python.exe -c "import z3; print('z3', z3.get_version_string())"
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe .\run_tool_tests.py --suite all

Push-Location .\desktop
npm ci
npm run lint
npm run build
npm run check:resolved-state-proof
npm run check:materialization-proof
npm run check:migration-progress
npm run check:autopilot
Pop-Location
```

If Yarn Classic is not globally available for physical acceptance:

```powershell
npm install --global yarn@1.22.22
```

Block Sigma may be marked **DONE** only after this Windows gate is green. A fresh `representative production-like project` production-like run is the recommended final performance/operational acceptance, not an inner test-loop requirement.

## Final Windows repair loop вЂ” deferred cleanup telemetry

Status: completed; authoritative Windows rerun pending.

- Fixed a Windows-only deferred-cleanup telemetry collision where the event field `path` shadowed the positional telemetry sink argument and raised `TypeError: emit_verification_event() got multiple values for argument 'path'`.
- Cleanup telemetry now records the stale trial location as `cleanupPath`; cleanup remains best-effort and never weakens `WORKSPACE_TARGET_EXISTS` isolation.
- Added a platform-neutral regression that forces deferred cleanup and exercises the real telemetry writer.
- The external Block Sigma Windows gate was hardened separately to fail immediately on any non-zero native process exit code.

## Final Windows repair loop вЂ” Yarn scoped-workspace fixture escaping

Status: completed; authoritative Windows rerun pending.

- Fixed the Windows Yarn Classic physical acceptance fixture: the Python triple-quoted JavaScript used two source backslashes, which emitted an invalid `replace(/\/g, '/')` regex into `check-topology.js`.
- The fixture now uses four Python-source backslashes so the generated JavaScript contains the valid `/\\/g` regex that matches Windows path separators.
- Added `node --check check-topology.js` before package installation so future fixture syntax regressions fail immediately and are not misdiagnosed as verification-substrate failures.
- Linux validation parsed the fixture constant, generated the JavaScript, and `node --check` returned 0. The npm physical acceptance remains PASS locally; the Yarn Classic case is Windows-only and must be rerun by the authoritative Windows gate.
