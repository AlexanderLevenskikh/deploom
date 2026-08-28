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

## WS3 / P0-A — CLOSED (all three attacks, physically)

Mechanisms:
- (A) directory swap: memo hit requires (st_dev, st_ino) continuity of the
  watched root AND project path. Path identity != object identity.
- (B) hardlink alias: sealed trees are write-protected at seal time. Write
  protection is a property of the file OBJECT, so it is enforced through every
  alias. Plus a link-topology assertion (nlink == 1) on sealed/validation
  manifest passes, which costs zero extra I/O because the stat is already taken.
- (C) delivery race: an O(1) drain barrier. A sentinel is written under an
  excluded path inside the watched subtree; ReadDirectoryChangesW delivers in
  order, so seeing the sentinel's own event proves every earlier change was
  already delivered. Unconfirmed drain => authoritative fallback, never PASS.
- watcher uncertainty never fabricates CONTENT_MISMATCH; it falls through to
  authoritative validation with a typed reason.
- events under manifest-excluded paths no longer invalidate the lease.

Hot lease remains O(1): pinned by a test asserting zero whole-tree traversals.

Threat boundary (stated, not hidden): an owner may clear write protection.
That stops accidental/tool mutation and alias writes by any principal that
cannot rewrite the sealed file's attributes; it is not a defence against a
principal who owns the tree and deliberately unprotects it. Content detection
does not depend on the protection holding -- the adversarial tests defeat the
protection first and detection still fires.

## P1-A / P1-B — CLOSED
proof identity validates before stamping; benign metadata and watcher failure
no longer become content mismatches.

## Local-only repositories — FIXED (user-reported)
SOURCE_REMOTE_NOT_FOUND blocked baseline on a `git init` repo with no origin.
No remotes at all is now a local-provenance path (clean checkout + exact
sourceCommit + localOnly:true). A configured-but-missing remote still fails
closed. tests/regression/test_local_only_source_provenance.py

## WS6 — DONE  deploom_failure.py (DEPLOOM_FAILURE_V2)
Top-level domain boundary: expected failures exit 3 with a structured envelope
plus a human summary; only genuine defects are TOOL_INTERNAL_ERROR (exit 4).
Tracebacks go to a diagnostic artifact, never as the primary result. A generic
wrapper no longer masks the precise code.

## WS7 — DONE
build_artifact_tree_integrity now heartbeats (default 15s) with real state
(subphase, processed/total, elapsed). Every snapshot-publish substage emits
start/finish + durationMs.

## WS8 — MEASURED
Measured on this machine: integrity seal = 795 entries/sec, per-file latency
bound (838 MB of large files hashed at ~240 MB/s, so SHA-256 is not the limit).
Extrapolates to ~189s for a 150k-entry tree. The dominant publish cost is
build_artifact_tree_integrity, which ran AFTER the "zero-copy promotion
complete" message with no output at all -- that is the 704s silence. Also
measured: inventory_reparse_plan is a separate whole-tree walk (2.19s/12.4k
entries) executed by several stages over the same tree.
Fixed: attribution + heartbeats. NOT done: reducing the traversal count.

## WS9 — DONE
reap_orphaned_source_snapshots(): bounded, age-thresholded, rename-first
(the rename is itself the liveness test), never touches this process's own
containers, uses _force_rmtree so write-protected sealed trees are actually
removable. On first run it reaped 8 real orphans from this machine.

## WS10 — DONE
Certified generalized clauses are bound to a fingerprint of the finite domains
they were certified over, and revoked at iteration start if those domains move.

## NOT DONE
- Production-like physical acceptance on a real Yarn Classic project.
- PreparedArtifact write-protection (see final report: deliberate, explained).
- Reducing duplicate whole-tree traversals in snapshot publish.
