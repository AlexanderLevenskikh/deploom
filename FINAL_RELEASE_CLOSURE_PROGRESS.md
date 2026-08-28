# FINAL RELEASE CLOSURE — PROGRESS

Base HEAD: 5e7e72b115486e191afbc4286e393b785b47d3bd (v0.2.71, clean tree)

## WS1 — ROOT CAUSE: OBSERVED_RESOLVED_ASSIGNMENT_ESCAPE — DONE (proven physically)

`_installed_package_json_path` (baseline_constraint_verifier.py) required
`candidate.resolve()` to stay inside the trial root. Under
`isolation=ntfs-junction-guarded` every package in node_modules is an NTFS
junction into the sealed PreparedArtifact (prepared_workspace_fastpath.py:771
`pairs.append((destination, entry.resolve()))`), which lives OUTSIDE the trial
root by construction. `Path.resolve()` traverses junctions, so the boundary
guard fired on the first sorted managed direct dependency. With 77 direct deps
that is `@babel/cli` ('@' sorts before letters) — the package name was
incidental, not causal. Every one of the 1405 junctioned packages would escape.

Reproduced physically against unmodified production code; error string matched
the incident byte-for-byte.

Classification path: ObservedResolutionError -> BaselineVerifyResult(kind="unknown")
-> BASELINE_VERIFY_UNKNOWN_ERROR. Exactly the observed message.

## WS2 — FIX — DONE
- `_GuardedClone.authorized_lower_roots` + `guarded_clone_authorized_roots()`.
- Authoritative read set = trial root + sealed lower roots the Ω plan actually
  mapped. Anything outside remains ESCAPE. Version equality still enforced.
- MANDATORY PRE-CHECK inserted before project process launch: drift/escape now
  quarantines the materialization, does ONE controlled rebuild through the
  authoritative full-copy path, and fails closed as SUBSTRATE_ASSIGNMENT_DRIFT
  (kind="infrastructure" => no Solver learning) if it recurs.

Tests: tests/acceptance/test_guarded_lower_assignment_escape.py (5/5, real
junctions + real Ω materializer).

## NOT DONE — see final report
WS3 (lease soundness), WS4 (proof identity census), WS5 (artifact continuity),
WS6 (DEPLOOM_FAILURE_V2), WS7 (heartbeats), WS8 (704s perf), WS9 (reaper),
WS10 (domain fingerprint), production acceptance.

## WS3 — PARTIAL
Closed (physically proven to fail on HEAD, pass after fix):
- directory swap: memo hit now requires (st_dev, st_ino) continuity of the
  watched root AND project path. Path identity != object identity.
- watcher failure / benign event: watcher uncertainty no longer fabricates
  SOURCE_SNAPSHOT_CONTENT_MISMATCH. It retires the memo and falls through to
  authoritative full validation, which decides. Telemetry records the reason
  (root-missing | directory-identity-changed | watcher-failure |
  watcher-thread-dead | watcher-event-observed).

STILL OPEN: hardlink alias mutation, watcher delivery race. No drain/generation
protocol and no link-topology lease exist. P0-A is NOT fully closed.

## P1-A — CLOSED
build_verification_proof_identity now validates the active snapshot before
stamping its key, and resolves the registry lookup from the original logical
path before any rebind. Regression counts real validation calls (was 0).

## NOT DONE
WS5 PreparedArtifact continuity (P0-B), WS6 DEPLOOM_FAILURE_V2, WS7 heartbeats,
WS8 704s measurement, WS9 reaper, WS10 domain fingerprint, production acceptance,
platform matrix beyond Windows.
