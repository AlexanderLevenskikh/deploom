# DepLoom performance report

Optimizes the user-critical path of the cumulative iterative migration
vertical loop (the Desktop control plane) while preserving all correctness
guarantees (verification, audit, UNKNOWN staying UNKNOWN, failing closed).

Implementation SHA under review: `96a09731dd2a51ce451b783f22ab8912c91c2903`
(clean before-branch). All numbers below come from the harness in
`benchmarks/`; raw results live in `benchmarks/work/results/` (git-ignored).

## Result summary

End-to-end vertical loop, `iterative_migration.py` CLI, one fresh Python
process per step (identical to Desktop). Medians over an interleaved
before/after A/B on the same machine:

| Scenario | Before (median, s) | After (median, s) | Delta | Git subprocesses (/run) |
| --- | ---: | ---: | ---: | ---: |
| cold | 23.26 | 20.66 | **−11.2%** | 108 → 20 |
| repeat-identical | 27.72 | 22.09 | **−20.3%** | 108 → 20 |
| leaf-change | 14.49 | 11.34 | **−21.7%** | 58 → 10 |

Per-run samples (interleaved)

* Before: cold {23.27, 23.04, 23.26, 23.53, 21.62}; repeat {28.71, 26.72};
  leaf {13.60, 15.37}.
* After:  cold {20.66, 21.00, 20.68, 18.60, 19.08}; repeat {20.42, 23.75};
  leaf {11.36, 11.32}.

The before/after distributions for each scenario do not overlap — the win is
above machine noise on this Windows host.

## How the time was found

1. **Instrumentation** (`runtime_metrics.py`, opt-in via `DEPLOOM_METRICS=1`,
   no-op otherwise, wired in `iterative_migration.py:main`): per-step wall
   time plus subprocess counts by kind, git by subcommand, argv samples.
   Emits one `ITERATIVE_MIGRATION_METRICS_V1` envelope per CLI process.
2. **Harness** (`benchmarks/bench_iterative_migration.py`): seeds an npm
   cache once from the real registry mirror, then runs all installs offline
   (`npm_config_offline=true`) for deterministic repeats.
3. **Profile** (`benchmarks/profile_verify.py`, cProfile of one
   `verify-exact`): 108 `git -C … rev-parse --show-toplevel` +
   `rev-parse HEAD` probes per warm-ish run dominated the cheap overhead
   (~27–30 ms each → ~3.2 s of a 21.5 s run). All 108 were **read-only
   identity probes**, repeatable per process.

## Changes

### 1. One combined git probe + process-local memoization of subject layout

`source_snapshot._subject_layout` runs `rev-parse --show-toplevel` and
`rev-parse HEAD` as two subprocesses. Replaced with a single
`rev-parse --show-toplevel HEAD` call that preserves both error modes
exactly (toplevel-empty → not-a-repo; toplevel-ok + failed HEAD →
`SOURCE_GIT_HEAD_UNAVAILABLE`, including the empty-repository case), and
memoizes the (root, relative, head) result per resolved directory. Safe
because `source_snapshot` never writes to the probed repositories within a
process (no in-process `init`/`commit`).

### 2. Shared memoized toplevel probe (`git_identity.py`)

The same toplevel probe was duplicated in `verification_proof._git_root_or_none`
(preserving its `.git`-marker / 127 raise semantics), `resolved_dependency_state
._git_root_or_project`, `prepared_workspace_fastpath._git_root`,
`project_topology._git_layout`, `baseline_constraint_verifier
._git_root_and_relative` and `dependency_live_roadmap_generator
._proof_source_head_clean_and_entries`. All now call one shared, memoized
`git_identity.git_toplevel`/`git_toplevel_raw` with identical success
predicate; each caller keeps its own error behavior. No DepLoom code creates,
moves or commits a repository at probed paths after they are first probed in a
process, so the cache cannot go stale.

### 3. Synchronous small-tree hashing (removes ThreadPoolExecutor churn)

`_build_source_tree_manifest_impl` spun up a `max_workers=8` pool and waited
on its wakeups even for tiny trees. Profiling showed hashing is cheap in this
fixture (340 `_hash_regular_file` calls, 0.056 s) while the pool machinery
(`drain_one`/`__exit__`/`shutdown`) cost ~1 s per `verify-exact`. Trees with
at most `_SYNC_HASH_THRESHOLD = 256` regular files are now hashed
synchronously in deterministic order with the identical
`_hash_regular_file` (stability + link checks intact); larger trees fall
back to the original bounded streaming pool (at most `max_pending` in
flight), preserving the million-file memory/future bound. Executor shutdown
is guaranteed even on exception.

## Correctness evidence

* New regression/equivalence tests: `tests/test_git_identity_fast_paths.py`
  (10 tests) — combined-probe semantics on normal/empty/non-repo/nested
  projects, memoization (no re-probe), shared probe marker semantics, and
  manifest **byte-identity** across sync / flushed / all-executor paths for
  small trees and a 300-file overflow tree.
* Targeted suites for every touched module pass (216 tests): source snapshot
  (stability, git-admin-churn, reaper, lease, source-truth), verification
  proof / identity, project topology, prepared-workspace fastpath, resolved
  dependency state, baseline constraint verifier, dependency roadmap, repair
  capture, guarded-lower link topology.
* Canonical gate `python run_tool_tests.py --suite all`: 1688 tests,
  **2 failures are pre-existing and environmental**, NOT introduced here —
  confirmed by reproducing both on an unmodified `HEAD` worktree:
  1. `test_cutoff_envelope_in_subprocess`: passes with `PYTHONUTF8=1` (wrong
     console code page mojibake in a subprocess string).
  2. `test_real_red_control_cannot_publish_upgrade_evidence`: fails on
     baseline too (a `project-check` running `node -e "process.exit(1)"` is
     reported PASS on this host; unrelated to git probes/hashing).

## Reproduce

```powershell
# bench + A/B (see benchmarks/README.md for DEPLOOM_BENCH_ROOT usage)
python benchmarks\bench_iterative_migration.py --scenario all --reps 3
# per-step breakdown
python benchmarks\analyze_results.py --from benchmarks\work\results\iterative-<stamp>.json
# single verify-exact cProfile
python benchmarks\profile_verify.py
```

Raw data: `benchmarks/work/results/iterative-20261004-201330.json` (baseline),
`iterative-20261004-203055.json` and later files (after), plus the interleaved
series `-203600/-203744` (A/B cold), `-204134/-204259` (repeat),
`-204348/-204439` (leaf). Profile evidence: `benchmarks/work/verify-exact.prof`.

## P2 backlog (not changed here)

* `npm --version` / `node --version` probes in
  `verification_proof._version_identity` (~0.35 s per verify-exact; part of
  the resolver-context identity, so removal would change identity semantics).
* Large text reads (`TextIOWrapper.read` ~0.6 s) and pathlib `.resolve()`
  WMI/`GetFinalPathNameByHandle` calls (~840/verify) — need a dedicated
  micro-benchmark to confirm real savings before touching.
* Python cold-boot + heavy imports per CLI process (~120 ms − 15 process
  spawns) — lazy imports for cheap subcommands.
* Robocopy `/E /COPY:DAT` copies of the source tree on every capture step
  (several seconds; moving away from full-tree copy needs a copy-less
  materialization design).

## Refuted hypotheses

* "Per-step wall is dominated by npm/node within the steps" — true for npm but
  the *cheap repeated* overhead was the git identity probes.
* "Hashing files is the hot cost" — false here: 340 hashed files cost 0.056 s;
  the ThreadPoolExecutor wakeup machinery cost ~1 s.
* "The fast path slows mid-size trees by dropping to one thread" — no: trees
  above the 256-file threshold keep the streaming pool, and the overflow
  differential test asserts identical manifests either way.
