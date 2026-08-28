# Changelog

## 0.2.1 — First public DepLoom release

- First public release from a sanitized, fresh-history source snapshot; private/internal Git history is intentionally not part of the public repository.
- Baseline structural verification now compares normalized diagnostics, confirms project-derived dependency evidence before learning constraints, and covers known ESM/CJS, TypeScript module-resolution and toolchain-runtime incompatibility classes before Executor.
- Post-Executor dependency contradictions feed deterministic evidence back into localization and exact Z3 instead of becoming an LLM version-repair loop.
- Added immutable materialization proofs and watchdog/heartbeat progress for long Baseline localization/reproduction work, including process-tree termination on hard stalls.
- Public auto-update feed is pinned to GitHub Releases in `AlexanderLevenskikh/deploom`; packaged clients check shortly after startup and every 30 minutes, auto-download valid updates, and install on quit or explicit user action.
- Removed private GitLab CI configuration and temporary development patch artifacts from the public source snapshot.
- Rebranded the installed application identity to `DepLoom` (`io.github.alexanderlevenskikh.deploom`) before the first public installer, including installer/artifact names and Windows AppUserModelId.
- Added supported Linux x64 AppImage packaging with `latest-linux.yml`, platform-correct bundled Z3 verification and public GitHub Release upload.
- Added macOS x64/arm64 DMG+ZIP CI packaging/smoke verification; macOS artifacts remain intentionally unpublished until Developer ID signing and notarization are configured.
- Desktop command execution is now platform-neutral: `python3` on POSIX, dedicated POSIX process groups for complete child-tree termination, platform-aware icons, and case-correct path containment.
- Clarified that the Python entry points are advanced/headless core tooling, not a second full Autopilot orchestration implementation.

## 0.1.132 — Baseline watchdog, progress and structural runtime hardening

- Replaced verifier `subprocess.run(timeout=...)` with a heartbeat-enabled process runner that owns and terminates the complete child process tree. Windows `cmd -> yarn/npm -> node` descendants can no longer keep localization workers alive indefinitely after a timeout.
- Added a hard wall-clock watchdog for each isolated assignment verification attempt, plus a bounded total localization watchdog. Timeout/infra paths preserve the dependency plan and never learn a constraint.
- Baseline localization now emits wave/check/heartbeat/shrink/reproduction progress and writes an atomic `.dependency-roadmap/state/baseline-verification-progress.json` checkpoint, so long deterministic work is observable and its last phase survives diagnostics.
- Desktop marks two minutes of generator silence as a possible stall and hard-stops a generator process tree after fifteen minutes without heartbeat/output.
- Auto-discovered adaptive Baseline checks now include `build` and `test:unit`/`test` (with build-before-test ordering), while ordinary source failures remain non-authoritative. This closes the previously observed Vitest/Vite/Sass compatibility gap.
- Added the reproduced `sass.initAsyncCompiler is not a function` toolchain-runtime structural signature, still gated by candidate-vs-control differential comparison and two fresh proof reproductions before Z3 can learn from it.
- Vite/Vitest generated caches are normalized before every Baseline project check, preventing stale test/build caches from masquerading as dependency evidence.
- Added regression contracts for localization progress/watchdogs, process hard timeouts, cache normalization, Sass runtime evidence, and Desktop stall markers.

## 0.1.131 — Deterministic compatibility feedback and materialization proofs

- Post-Executor `replan-required` is now a deterministic dependency-feedback event: Desktop persists structured evidence, preserves the branch, reproduces candidate versus pre-update dependency control, delta-localizes the structural regression, requires stable reproduction, and only then feeds a session-local nogood back to exact Z3.
- Added machine-level dependency materialization proofs covering the exact assignment, dependency sections, lockfile digests, Node/package-manager identity and materialization commit. Manifest-only target presence is no longer sufficient to split semantic batches.
- Executor dependency mutations are detected after every model call and deterministically restored from the materialization proof without discarding legitimate source/config edits. Dirty dependency state at v2 recovery no longer downgrades ownership back to the LLM.
- Resolver and project Baseline verification now promote one immutable verification workspace through resolver/lifecycle/project phases, removing a redundant clone and resolver install for successful assignments.
- Agent admission now reserves explicit provider/system/tool-history and output headroom in addition to the application input cap.
- Structural Baseline rejection logs name the newly introduced structural signatures separately from the full set of red commands.
- Added deterministic contracts for post-Executor compatibility evidence, materialization proof tamper detection, and deterministic Supervisor routing.

## 0.1.130 — Baseline structural proof and repair-loop hardening

- Baseline project preflight now runs every configured structural command instead of stopping at the first red check, so a normal type-migration error can no longer mask a later Stylelint/ESM compatibility failure.
- Adaptive project compatibility is differential by structural signature rather than whole-command exit code. New exports/module-resolution failures are detected even when the same baseline command is already red for an unrelated source migration.
- Added stable structural signatures for ESM/CJS loader boundaries, duplicate type universes and TypeScript exports-only/moduleResolution failures such as `@vitejs/plugin-react` under legacy `moduleResolution: Node`.
- Project-derived solver nogoods require two fresh reproductions with a stable structural signature before they can influence the exact assignment; inconclusive/flaky failures are never learned.
- Deterministic group verification removes stale generated Vite/Vitest caches before gates, eliminating LLM repair cycles whose only effective action was deleting `.vite`/`.vitest`.
- Live base-branch gate evidence is shared across parallel groups and the full gate set is probed once per FLOW job instead of reinstalling/probing the same baseline independently for every group.
- Legacy `solverBackend=custom` workspace settings can no longer restore heuristic production authority: production always forces exact Z3, and the undocumented `referenceOnly` configuration escape is ignored.
- Persistent resolver proof fingerprints now include the effective Node and package-manager executable/version in addition to manifests, lockfiles, registry and platform.

## 0.1.125 — Interaction IR and platform-correct Z3 packaging

- Added a verification-only dependency Interaction IR. Published `dependencies`/`optionalDependencies` between two project-direct packages now create `direct-shadowing` evidence without being mis-modeled as hard peer/equality constraints.
- Conflict localization keeps hard peer/nogood cohorts atomic but adds one-hop direct interaction context, so a package-manager-green/project-red pair can teach an exact nogood such as `NOT(vitest@3.2.6 AND vite@4.3.9)` while avoiding giant transitive verification units.
- Added structural TypeScript duplicate-type-universe detection for diagnostics that reference two physical `node_modules` copies of the same package; ordinary application API errors remain migration work.
- Split native `z3-solver` from portable Python requirements. Windows packaging now vendors a Windows Z3 wheel explicitly instead of filtering it out with `--platform any`; Linux CI validation uses a native solver vendor.
- Kept the exact Z3 backend authoritative while preserving a clean separation between hard constraints, verification couplings, and learned constraints.

## 0.1.124 — Planner/Executor boundary hardening

- Made Z3 the authoritative default dependency solver; exact timeout/UNSAT defers only the affected independent component instead of falling back to heuristic search or stopping the whole run.
- Added deterministic pre-Executor materialization of the complete immutable dependency assignment and lockfile.
- Separated dependency compatibility cohorts from bounded semantic LLM batches after materialization, so a large cohort no longer implies a giant agent prompt.
- Added a hard application-owned agent context budget and stable batch-completion markers so package targets alone cannot be mistaken for completed Executor work.
- Removed dependency-version, override/resolution, and global replan decisions from Executor/repair prompts; dependency contradictions return structured evidence to Planner/Verifier.
- Replaced full-group verification repair prompts with compact diagnostic repair prompts carrying only the immutable assignment, failing evidence, and verification contract.
- Fixed lifecycle-install failure classification so transient infrastructure/dependency failures cannot be mislabeled as project evidence and learned as false solver constraints.

## Unreleased — Pre-executor plan hardening

- Moved deprecated `@types/*` stub removal into deterministic planning and couples removal atomically with the runtime upgrade that proves replacement type declarations.
- Added conservative tarball entrypoint validation so broken publishes with missing declared `main`/`bin` files are rejected before Executor.
- Added adaptive Baseline project preflight for small type/style/script checks: new module-loader incompatibilities can refine the solver, while ordinary source migration errors remain Executor work.
- Adaptive structural evidence is compared by stable per-command signatures against the current baseline; an unrelated pre-existing red diagnostic no longer masks a newly introduced structural incompatibility.

## 0.1.111 — Public-release hardening

- Removed organization-specific project names, package scopes, registries, CI mirrors and local paths.
- Added a public sanitization/security gate and reviewed-binary allowlist.
- Hardened GitHub Actions by pinning third-party actions to immutable commit SHAs.
- Generalized project-aware dependency heuristics to structural project characteristics.

## 0.1.110 — Constraint-aware planning

- Added solve → verify → learn-constraint iteration before migration branches are created.
- Added conflict-directed version search and parallel conflict localization.
- Separated plan, infrastructure and migration failures.
- Added GitHub Actions CI/release support and public GitHub updater support.

> Earlier development history is intentionally omitted from the public changelog because it contained organization-specific incident and infrastructure details.
