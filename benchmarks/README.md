# DepLoom performance benchmarks

## Retained-state verification

`python benchmarks/bench_verification_reuse.py --reps 3` uses the real
`verify_assignment` entry point on an isolated npm/Git fixture. It retains
one project's proof/preparation caches across cold verification, an identical
warm repeat, a committed leaf-source change, a root manifest/check-profile
change and a dependency assignment change. Every sample requires authoritative
PASS and evidence that the mandatory shell check actually executed. Raw rows
include resolver/cache/check counts and CPU/RSS snapshots when available.

These are verification measurements, excluding online security audit, AI
latency and Electron startup/rendering. The iterative CLI harness below also
excludes those stages. Neither harness establishes full product latency on a
representative real project; those measurements remain follow-up work.

## Fresh iterative runs (historical scenario names)

The names `cold`, `repeat-identical` and `leaf-change` are retained for old
commands. They mean fresh proof caches, a fresh run on a warm host, and a
fresh replan with one fewer target, respectively. They do not demonstrate
warm proof reuse or incremental invalidation of an existing run. The npm
cache is seeded in all three, so `cold` is not an empty-machine-cache run.
Samples now include platform/Python/source-digest metadata. With few repeats,
report the full range; the reported nearest-rank p95 is not a reliable tail
latency estimate for such a small sample.

Reproducible benchmark harness for the DepLoom user-critical path. Everything
in `benchmarks/` is tracked; scratch work, fixtures and raw results live under
`benchmarks/work/` which is git-ignored.

## What is measured

The cumulative iterative migration vertical loop (`iterative_migration.py`
CLI, one fresh Python process per step — exactly like the Desktop control
plane):

* `cold` — full vertical loop from a fresh run-dir + fresh byte-identical
  project clone (no proof cache).
* `repeat-identical` — same loop against a fresh run-dir + fresh byte-identical
  project clone (warm pyc / npm / OS file cache; fresh proof cache). Quantifies
  how much an identical re-run repeats work.
* `leaf-change` — same loop but one leaf target is dropped from the targets
  file (mirrors "user changed one dependency" replan).

Per-step wall time and subprocess/git/node/npm/robocopy counts are recorded
when `DEPLOOM_METRICS=1` is set (the harness sets it). All runs are
deterministic offline after a one-time npm cache seed (real registry mirror
required once).

Supporting tools:

* `analyze_results.py --from <raw.json>` — per-step breakdown of a raw run.
* `profile_verify.py` — cProfile of a single `verify-exact` step.
* The opt-in `DEPLOOM_METRICS=1` instrumentation in `runtime_metrics.py`
  (no-op unless the env var is set) emits `ITERATIVE_MIGRATION_METRICS_V1`.

## Running

```bash
python benchmarks/bench_iterative_migration.py --scenario all --reps 5
```

`--reps` controls how many times each scenario is re-run (median + p95 are
reported). `--keep` keeps the existing npm cache seed / fixture stage.

## Before/after A/B

Point the harness at a different checkout of the production CLI (e.g. a
baseline worktree) while keeping the bench stage/npm-cache under this repo:

```bash
git worktree add --detach <tmp> HEAD          # baseline checkout
$env:DEPLOOM_BENCH_ROOT = "<tmp>"
$env:DEPLOOM_BENCH_PYTHON = "$PWD\.venv\Scripts\python.exe"
python benchmarks\bench_iterative_migration.py --scenario cold --reps 3
Remove-Item Env:DEPLOOM_BENCH_ROOT
python benchmarks\bench_iterative_migration.py --scenario cold --reps 3
```

`DEPLOOM_BENCH_ROOT` overrides where `iterative_migration.py` is launched
from; `DEPLOOM_BENCH_PYTHON` overrides the interpreter. This lets you compare
a candidate change against `HEAD` back-to-back on the same machine. A baseline
checkout has no metrics instrumentation, so its git/npm/node counters read 0 —
compare wall time, not counters.
