# Working on DepLoom

These instructions apply to this repository. Follow the user's task scope and preserve unrelated work. A review or documentation task does not implicitly authorize publishing a release. When publication is already authorized, use the release workflow below without repeatedly asking for permission.

## Start here

- Read `git status --short` and recent commits before editing. Other agents may be working in the same checkout; record the implementation SHA you review and recheck HEAD before reporting results.
- Read the relevant code, tests and scripts. `README.md`, `desktop/README.md`, `HOW_IT_WORKS.md` and `settings.schema.json` explain the product; dated documents in `docs/` are historical findings, not proof that today's implementation works.
- Distinguish this DepLoom source repository, a user workspace with `.dependency-roadmap/`, and the projects being migrated. Never run a real migration or release against an arbitrary project just to test a helper.
- Prefer small changes at the owning layer. Avoid extending the large entrypoints when a focused existing module owns the behavior.

## Repository map

| Area | Responsibility |
| --- | --- |
| `dependency_live_roadmap_generator.py` | Python CLI, settings/path resolution, baseline planning, roadmap/dashboard artifacts and Draft prompt generation (`build_draft_prompt`). |
| `peer_solver_*.py`, `baseline_constraint_verifier.py`, `constraint_verify.py`, `block_psi*.py` | Dependency solving, candidate verification, learning, progressive search and recovery. |
| `source_snapshot.py`, `resolved_dependency_state.py`, `prepared_workspace_fastpath.py`, `verification_proof.py`, `verification_workspace_backend.py` | Source/dependency identities, isolated materialization, reusable verified state and proof boundaries. |
| `manual_dependency_audit.py`, `validate_dependency_update.py`, `validate_dependency_regression.py` | Independent vulnerability/lag audit and update/regression checks. |
| `dependency_audit_branch.py`, `dependency_release_branch.py` | Git orchestration for migrated projects; distinct from publishing DepLoom itself. |
| `desktop/electron/main.ts`, `preload.cts`, sibling modules | Electron control plane, IPC, subprocesses, FLOW/Autopilot, agents, recovery and publication. Repair is split into `repair-dispatch.ts`, `repair-checkout.ts`, `repair-handoff.ts` and `baseline-repair-result.ts`. |
| `desktop/src/` | React/TypeScript UI, components, policy presentation and localization. Keep runtime authority in the owning backend, not UI labels. |
| `tests/`, `desktop/scripts/check-*.mjs` | Python unit/regression/acceptance suites and Desktop contract/behavior checks. |
| `scripts/`, `.github/workflows/`, `desktop/scripts/prepare-tool.mjs` | Public sanitization, packaging, CI/release and bundling the Python core with Desktop. |

`desktop/dist/`, `desktop/dist-electron/`, `node_modules/`, caches and `.dependency-roadmap/` are generated/local data. Edit source and rebuild; do not fix generated JavaScript. Keep private project artifacts out of public docs and commits.

## Correctness contracts

- Draft is a proposal. Resolver success, a sealed source snapshot, a search incumbent and project-green verification are different states. An exit code of zero or a mode named `green` does not prove the project checks passed.
- Preserve the last verified cumulative checkpoint. An accepted cohort builds on verified cumulative state; deferred work remains visible in the health denominator and remaining work.
- Infrastructure failures, unavailable evidence, timeout and solver UNKNOWN must not become false incompatibility constraints or zero vulnerabilities. Never weaken existing checks, hooks or acceptance thresholds to manufacture green.
- Keep target policy, source identity, dependency assignment and proof identity aligned across planning, execution, audit and release. Agent prose cannot grant verification authority.
- Source/config repair works in an isolated checkout. Keep the ordinary source guard enabled; repair capture is explicitly pinned. Re-verification must use repaired bytes and fresh evidence, and the authoritative verifier closes the exact repair request.
- Repair settings must preserve the original workspace base and relative path semantics. Desktop and Python must read/write the same durable handoff. Test the actual path resolver when changing settings placement.
- Persist agent session IDs before the agent finishes. Distinguish a new attempt, resuming an in-flight agent and resuming verification. Restart must not spend another attempt or abandon the last in-flight attempt.
- Exercise production boundaries, including filesystem/Git and Python-to-Node handoff where relevant. A fake verifier passing does not establish a working real Desktop migration. Tests must be reproducible without ignored local audit artifacts.

## Prompts and migration deliverables

When changing Draft/migration prompts, preserve both RU and EN contracts:

- Request intermediate summaries after meaningful cohorts: actual updates/deferred work, lag percentage with denominator/coverage, vulnerability counts and unknown evidence, checks and next steps.
- Use `manual_dependency_audit.py` with the current policy and saved JSON/Markdown evidence. Consult its `--help` and existing invocation builders; do not substitute a bare `yarn audit`. Yarn `auto` currently uses an isolated npm-lock bridge with inventory/evidence guards.
- Ask for explanations of non-obvious source/config changes. Do not insert invalid comments into JSON or generated lockfiles; explain those by file/key in the report.
- Require `docs/dependency-migration/<run-id>/DEVELOPER_UPGRADE_GUIDE.md`: capabilities of actually installed upgrades, constraints, breaking changes and advice for future development.
- Require `docs/dependency-migration/<run-id>/MIGRATION_REPORT.md`: old/new versions, changes, breaking behavior, adaptations, validation, audit metrics, remaining blockers and evidence links.
- Document actual upgrades, not merely proposed targets. Verify that deliverables exist; do not assume Baseline CLI finalization creates agent-authored documents.

## Validation

Use the narrowest meaningful tests while iterating, then complete the checks required for the changed surface. Report exact commands, results and limitations, without reusing an earlier test count as evidence for a new commit.

- Python dependencies: `requirements.txt` and `requirements-solver.txt`. CI uses Python 3.12 and Node 22; Desktop declares its Node requirement in `desktop/package.json`.
- Canonical Python runner, from repository root: `python run_tool_tests.py --suite all`. It covers unit, regression and acceptance; `--suite production-fast` is a SEPARATE CI gate, not included in `all`. Use `--list` to inspect collection. Free functions need appropriate unittest collection; a file existing does not mean its tests run.
- Desktop, from `desktop/`: `npm ci` when dependencies need installation, `npm run lint`, `npm run build`, then relevant `npm run check:...` scripts. Many checks import `dist-electron`; compile with `npx tsc -p tsconfig.electron.json` or build first.
- Repair changes: run `check:repair-handoff`, `check:repair-dispatch` and, from root, `python -m unittest discover -s tests/regression -p test_source_checkout_repair_capture.py -v` plus `python -m unittest discover -s tests/regression -p test_repair_settings_workspace_base.py -v`. Include restart on the last attempt and real checkout/settings boundaries.
- Before public publication: `python scripts/check-public-sanitization.py`, applicable Python suites, release asset checks and Desktop gates. `.github/workflows/ci.yml` and `PUBLIC_RELEASE_CHECKLIST.md` define the current broader checks. The release helper runs Desktop gates but does NOT run the Python suite for you.
- Sanitization enumerates Git-tracked files: a pass before adding new documentation does not cover that documentation. Check the complete staged/release surface after final edits, including reports. Never suppress the denylist to publish private project evidence.
- `spawn EPERM`, Windows Temp access failures and sandbox-denied Git subprocesses are not automatically application defects. Inspect the real child error and use an approved rerun when needed; never bypass the sandbox or call an unexecuted test green.
- After edits run `git diff --check`, inspect the actual diff and explicitly inspect new untracked files (ordinary `git diff` omits them).

## Publishing DepLoom: use the release scripts

For an authorized public release, the normal entrypoint from the repository root is:

```powershell
.\push-github-branch-and-tag.ps1 -Commit "fix(area): describe the actual change"
```

This wrapper enforces `master` and the public SSH origin `git@github.com:AlexanderLevenskikh/deploom.git`, then calls `push-branch-and-tag.ps1`. The lower-level helper accepts `-Remote`; use the public wrapper for this repository's normal release.

- Omit `-Tag` for a normal patch bump. Use `-Tag vX.Y.Z` only for a deliberately chosen version or retry of the SAME partially published release. Do not paste historical version examples from old docs.
- Do not manually duplicate the helper with ad hoc version edits, release commits, `git tag` and `git push`. It creates a source commit if needed, synchronizes `VERSION`, `desktop/package.json` and `desktop/package-lock.json`, creates the version commit, validates that exact commit in a clean temporary checkout, pushes the branch, creates/reuses an annotated tag and pushes the tag.
- The helper stages almost all repository changes (`git add -A` with noise exclusions). Inspect ALL tracked and untracked work first; do not accidentally include another agent's files. If package manifests have intentional non-version edits, commit those deliberately before invoking the helper: it requires the three version-bearing files to be clean before its bump.
- Keep validation enabled. Do not silently use `-SkipValidation` to evade a failing gate. The helper runs sanitization, release asset checks, Desktop install/lint/build and every `check:*` script it discovers; Python checks still need separate execution.
- If interrupted or failed, inspect HEAD, worktree, local tag and remote branch/tag before retrying. Branch push and tag push are separate operations. Retry a partial publication with the SAME explicit `-Tag`; omitting it can create an unintended new patch version. Do not force-move published tags or rewrite published history.
- The helper has guarded local rollback logic. Never imitate its rollback with a manual destructive reset; preserve concurrent edits and inspect any recoverable failure state first.
- Tag publication starts `.github/workflows/release.yml`. Report source SHA, release SHA/tag, push result and observed CI/release state separately. A successful push does not mean installers were built or the real-project migration passed acceptance.

## Native Windows editing policy

- Never invoke `apply_patch`, its batch wrapper or the Codex apply-patch entrypoint; do not retry or request broad filesystem access to work around it.
- Prefer an accurate project-native generator/formatter. For patch-shaped edits, write a temporary unified diff INSIDE the repository, run `git apply --check`, then `git apply`, and remove the temporary patch.
- For small edits, read the current file and require each expected old fragment to occur exactly once unless multiple replacements are intended. Fail without writing on mismatch. Preserve encoding, BOM and dominant line endings; write through a temporary file and replace only after validation. Do not rewrite an existing file broadly for a localized edit.
- Modify files only inside the active workspace. Preserve dirty/unrelated changes. Never use `git reset --hard`, `git checkout --` or `git restore` to discard work.
- Use native PowerShell filesystem operations with literal paths; before recursive cleanup verify the resolved absolute target is the intended owned workspace directory. Do not mix shells for deletion or move operations.
- Prefer `rg` / `rg --files` for searches. Use UTF-8 explicitly for generated documentation and Python console output when Unicode is needed. Keep output focused and never print credentials.
