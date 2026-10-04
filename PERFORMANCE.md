# DepLoom performance and correctness report

This report supersedes the 0.2.186 performance claims. That report used fresh
run directories for its historical `repeat-identical` and `leaf-change`
scenarios, so those numbers did not establish warm proof reuse or incremental
invalidation. Its path-only Git caches could also return stale repository/HEAD
identity. Those caches have been removed; the old 11-22% claim does not describe
the corrected implementation.

## Current measurements

Reference: `96a09731dd2a51ce451b783f22ab8912c91c2903` (before the performance
changes). Candidate: the source changes based on
`44e77bc0690b5c58cf900527cc5a50ea3f75129e`; its Python source digest and every
sample are published in [the sanitized measurement data](benchmarks/results/verification-and-cli-ab.json).
The release source commit containing this document identifies the reviewed
Desktop implementation. Version-only release changes follow that commit.

Three sequential interleaved A/B pairs, alternating order on one Windows host,
Python 3.14.3, real Git/npm/Node. The fixture is intentionally small. Installs
are offline after seeding npm's cache. No test suite or build ran concurrently.
The table reports median and full observed range in seconds; negative delta
means faster. Three samples cannot establish p95 or general product speed.

| Scenario | Reference median (range), s | Candidate median (range), s | Median delta |
| --- | ---: | ---: | ---: |
| verification:cold-verification | 4.461 (4.258-4.663) | 4.240 (4.114-4.511) | -4.9% |
| verification:warm-identical | 2.154 (2.082-2.625) | 2.326 (2.300-2.424) | +8.0% |
| verification:leaf-source-change | 3.246 (3.239-3.670) | 3.418 (3.344-3.863) | +5.3% |
| verification:root-config-change | 4.823 (4.274-4.969) | 4.612 (4.514-4.898) | -4.4% |
| verification:dependency-change | 4.582 (4.281-5.649) | 4.682 (4.675-5.092) | +2.2% |
| cli:cold | 27.692 (27.667-27.761) | 26.117 (25.465-26.361) | -5.7% |

The fresh iterative CLI loop is about 5.7% faster in this fixture. Individual
verification scenarios have mixed results, including a slower warm repeat and
leaf-source mutation. This is not a claim of universal acceleration. Restoring
live Git identity checks takes priority over the invalid cache speedup.

The retained-state harness keeps the same project and proof/preparation caches
within each five-scenario sequence: cold verification, identical repeat,
committed leaf-source change, manifest/check-profile change and dependency
change. Every sample requires authoritative `passed` and a recorded completion
of the mandatory project shell check. Cache/resolver/check event counts are
included in the published rows. The iterative CLI harness instead creates a
fresh run and fresh proof caches in every sample. Its baseline lacks metrics
instrumentation, so zero counters there mean unavailable data, not zero work.

Both harnesses exclude online vulnerability audit, model latency and Electron
startup/rendering. Whole-product latency on representative projects and
cross-platform large-tree profiling remain unmeasured. They are follow-up
work, not completed acceptance criteria of the original performance study.

## Safe changes retained

- Combine root and HEAD discovery into one live `rev-parse` call while retaining
  distinct not-a-repository and missing-HEAD failures.
- Share the Git root probe implementation without path-only result caching;
  new commits, repository creation and transient failures are observed anew.
- Hash up to 256 files synchronously with the existing stability/link checks;
  larger trees use the bounded executor. Cleanup also covers walk exceptions.
- Keep opt-in runtime metrics and add real retained-state verification scenarios,
  source/runtime metadata and a correct nearest-rank p95 calculation.

## Correctness and agent recovery

A physical Windows regression exposed a serious shell quoting problem:
`node -e "process.exit(1)"` could be reported PASS. The process supervisor now
preserves cmd.exe command syntax; quoted exit codes 0/1/7 and compound commands
with environment values and a spaced working directory are checked with real
Node children. The real red-control upgrade-evidence regression now passes.

The agent launch path now releases its dispatch lock on waiting/give-up returns,
separates user cancellation from timeout, handles structured provider errors
at exit zero, and parses duration/date/epoch reset windows. Billing/auth failures
terminate instead of waiting indefinitely. Provider session IDs and child PIDs
are saved during execution; retries/restarts retain the real session, database
and repair attempt. Waiting leases preserve their budget across long restarts.
Launch diagnostic credentials are redacted.

The Desktop lifecycle fixture executes the production handler with real Node
children and durable files, including 429, restart on the last repair attempt,
real trial file changes, cancellation, server outage and exhausted launch budget.
Its provider and Python endpoints are fixtures; it does not establish a real
external-model migration or a project-green result on a user's project.

Validation of these source changes, with `PYTHONUTF8=1`:

- `python run_tool_tests.py --suite all`: 1693 tests, OK, 4 skipped.
- `python run_tool_tests.py --suite production-fast`: 64 tests, OK.
- Both required source-checkout capture and repair-settings workspace-base
  regression commands: 4 and 2 tests, OK.
- Desktop lint/build and `check:agent-launch-errors`, `check:iterative-agent`,
  `check:iterative-stream`, `check:iterative-scenario`, `check:repair-handoff`
  and `check:repair-dispatch`: passed. The public release helper additionally
  checks the exact release commit in a clean checkout with every `check:*`.

The old report's two failing tests are not accepted as benign performance
exceptions: the shell-check defect is fixed, and subprocess Unicode output is
verified with explicit UTF-8. Mandatory checks and proof thresholds remain in
force.

## Reproduce and remaining work

```powershell
$env:PYTHONUTF8 = '1'
python benchmarks/bench_verification_reuse.py --reps 3
python benchmarks/bench_iterative_migration.py --scenario cold --reps 3
```

For A/B set `DEPLOOM_BENCH_ROOT` to the reference source checkout and
`DEPLOOM_BENCH_PYTHON` to the same interpreter for both revisions. See
[benchmark contracts](benchmarks/README.md). Local full raw artifacts stay in
the ignored `benchmarks/work/results/`; the published data omits private paths.

Next performance work requires profiles of online audit, Desktop event-loop/IPC,
large source capture/materialization, repeated resolver/check preparation and
solver cost on representative fixtures. Consider tool-version probe reuse only
with sound identity, lazy imports for cheap commands, and materialization reuse
only after preserving source and proof boundaries. No unmeasured multiplier is
claimed here.
