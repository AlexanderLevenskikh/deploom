# Block Lambda progress

Implementation status: complete; full gate and final adversarial repair loop passed.

This file is a resumable engineering checkpoint only. It is not runtime or proof authority.

## Workstream 1 — SourceSnapshot immutable consumption

Status: implemented; focused tests pass.

- Active snapshots are strongly rehashed before fingerprint, proof-subject access, materialization, and durable publication.
- Mutation invalidates the active process-local locator and raises typed SOURCE_SNAPSHOT_CONTENT_MISMATCH.
- Snapshot containers inside the captured subject fail with SOURCE_SNAPSHOT_TEMP_INSIDE_SUBJECT.
- Source regular hardlinks remain fail-closed.

Focused validation:

- tests.test_block_x_source_truth: 15 tests pass, 1 privilege-dependent symlink skip.
- Block Lambda mutation fixture: pass.

## Workstream 2 — PreparedArtifact coherence and hardlinks

Status: implemented; focused tests pass.

- Every load revalidates whole-tree content even after a process-local validation hit.
- Durable index continuity is rechecked after hashing; deletion/replacement is a miss.
- Regular files with st_nlink > 1 fail with PREPARED_ARTIFACT_HARDLINK_UNSUPPORTED.
- Cache observability records memory/durable/generation/content continuity.

Focused validation:

- tests.test_block_v_prepared_artifact: 6 tests pass.
- Same-process mutation, index deletion, and NTFS hardlink fixtures: pass.

## Workstream 3 — post-Executor identity

Status: implemented; focused tests pass.

- CompatibilityEvidence retains the ProofEnvelope SourceSnapshotKey separately.
- Exact exclusion/localization is gated by evidence == envelope == current Baseline SourceSnapshot identity.
- Mismatch is preserved as diagnostic-only telemetry and creates no solver authority.

## Workstream 4 — watcher and process supervision

Status: implemented; Windows physical focused tests pass.

- ReadDirectoryChangesW uses an OVERLAPPED request issued synchronously before start() returns.
- Windows processes start CREATE_SUSPENDED, attach to the Job Object, then resume via NtResumeProcess.
- Linux /proc token supervision is reported as best-effort, not guaranteed-tree.

## Workstream 5 — failure classification and unattended behavior

Status: implemented; full regression and aggregate gates pass.

- Arbitrary project command output no longer becomes infrastructure by broad regex.
- Failure signatures normalize trial paths, npm log paths, ISO timestamps, and PIDs while preserving package/version/range semantics.
- Yarn Classic required-package and engine predicates retain semantic literals.
- Diagnostic minimization UNKNOWN/predicate absence degrades to exact-only authority.

## Workstream 6 — failure paths and cleanup

Status: implemented; focused tests pass.

- check_result is initialized before project-check launch and reuse requires a real result.
- Old tool-owned verification trials have bounded age-safe reaping through atomic trash retirement.
- Source temp-inside-subject is rejected before materialization.

## Workstream 7 — proof identity

Status: implemented; focused tests pass.

- Production DEPLOOM_TOOL_BUILD_ID override no longer replaces computed semantic identity.
- Component coverage includes package manager, lockfile, resolved/proven state, solver model/transition/Z3, snapshots, artifacts, supervision, project verification, and evidence.
- Arbitrary-command ProjectProof PASS records are not reused durably without a declared external tool closure.

## Workstream 8 — release and physical gates

Status: implemented; workflow and release contract validation pass.

- Release has an exact-SHA validate-windows job.
- Windows packaging depends on validate-windows.
- CI and release Windows gates run Block Lambda physical fixtures plus acceptance.

## Focused adversarial fixtures

tests/test_block_lambda_release_closure.py covers:

1. active SourceSnapshot same-size mutation and stale locator
2. same-process PreparedArtifact mutation
3. durable index deletion
4. PreparedArtifact hardlink topology
5. triple evidence snapshot identity
6. failure signature noise stability and semantic distinction
7. Yarn Classic predicates
8. toolBuildId override rejection
9. orphan trial reaper
10. watcher arming boundary
11. Windows attach-before-execution metadata
12. Linux supervision-token scrub quality downgrade
13. diagnostic minimization INCONCLUSIVE preserves exact-only search authority

## Final validation

- python -m compileall -q .: PASS.
- Unit: 567 tests, OK (4 platform/privilege skips).
- Regression: 132 tests, OK.
- Acceptance: 12 tests, OK.
- Aggregate: 711 tests, OK (4 platform/privilege skips).
- Block Lambda Windows physical/adversarial module: 17 tests, OK (no skips).
- Desktop: npm ci, lint, build, and all 38 check:* contracts PASS; lint reports 3 non-fatal pre-existing warnings.
- Release: public sanitization, release assets, and Windows packaging resilience PASS.
- Final adversarial repair loop added post-copy SourceSnapshot manifest validation, deterministic scanner-handle closure, cleanup of rejected materializations, explicit machine observability events, and removal of the final graph-family diagnostic abort. No known P0 correctness or P1 release blocker remains.

## Files changed

- Source/artifact authority: source_snapshot.py, artifact_integrity.py, block_v_prepared_artifact.py, prepared_workspace_fastpath.py.
- Evidence/learning/failure handling: dependency_compatibility_evidence.py, dependency_live_roadmap_generator.py, baseline_constraint_verifier.py, constraint_cache.py.
- Supervision/proof identity: verification_process_supervisor.py, substrate_identity.py.
- Tests/release/docs: tests/test_block_lambda_release_closure.py, tests/test_baseline_constraint_verifier.py, tests/test_sigma_process_supervision.py, .github/workflows/ci.yml, .github/workflows/release.yml, README.md.

## Exact next action

Fresh independent adversarial public-preview acceptance.
