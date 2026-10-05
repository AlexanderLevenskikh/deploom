# Greedy scheduler: empirical results

Larger 96/192-package experiments with two-minute checks are documented in [GREEDY_LARGE_BENCHMARK.md](GREEDY_LARGE_BENCHMARK.md).

## Decision

Use size-aware scheduling by default: start at the cohort cap (24), exploration ratio 2, initial structural search budget 128. Grow exhausted cheap search within a hard limit of 4096 before spending another install/check. Keep cost-aware scheduling opt-in: these fixtures do not show a meaningful advantage over size-aware scheduling, and production agent/install-failure cost collection is incomplete.

## Equal-cost physical checks

One seed, real Node subprocess checks delayed by five seconds. Package compatibility is synthetic; counts below are fixture outcomes, not real-project migration proofs.

| Fixture | Strategy | Updates | Checks | Total seconds | First accepted seconds | Updates/min |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| clean | whole-first | 26/26 | 2 | 10.126 | 5.056 | 154.054 |
| clean | size-aware | 26/26 | 2 | 10.133 | 5.062 | 153.956 |
| large-remainder | whole-first | 25/26 | 16 | 81.343 | 10.192 | 18.44 |
| large-remainder | size-aware | 25/26 | 15 | 76.113 | 15.187 | 19.708 |
| cross-conflicts | whole-first | 20/26 | 3 | 15.302 | 10.198 | 78.421 |
| cross-conflicts | size-aware | 20/26 | 3 | 15.229 | 10.158 | 78.799 |
| dense-conflicts | whole-first | 19/26 | 3 | 15.366 | 10.153 | 74.188 |
| dense-conflicts | size-aware | 19/26 | 3 | 15.205 | 10.129 | 74.976 |

For the large remainder, size-aware scheduling improves total time but delays the first small checkpoint. It is a throughput choice, not a universal latency winner.

## Order sensitivity

Four seeds per fixture and strategy, delay disabled. These runs compare coverage/check counts only; their wall times are not representative of five-second verification.

| Fixture | Whole-first check counts | Size-aware check counts | Equal final coverage |
| --- | --- | --- | --- |
| clean | 2, 2, 2, 2 | 2, 2, 2, 2 | 26/26 |
| large-remainder | 16, 18, 14, 16 | 15, 15, 13, 15 | 25/26 |
| cross-conflicts | 3, 4, 3, 3 | 3, 4, 3, 3 | 20/26 |
| dense-conflicts | 3, 6, 3, 3 | 3, 4, 3, 3 | 19/26 |

## Companions and variable cost

Variable-cost checks delay five seconds plus 1.25 seconds for each upgraded slow package (up to ten seconds). Mandatory-companion failures request an exact companion instead of learning a false monotone conflict.

| Fixture | Strategy | Updates | Checks | Seconds | First accepted seconds |
| --- | --- | ---: | ---: | ---: | ---: |
| mandatory-companion | whole-first | 26/26 | 3 | 15.255 | 10.187 |
| mandatory-companion | size-aware | 26/26 | 3 | 15.251 | 10.186 |
| mandatory-companion | cost-aware | 26/26 | 3 | 15.193 | 10.134 |
| variable-cost | whole-first | 25/26 | 16 | 149.976 | 15.129 |
| variable-cost | size-aware | 25/26 | 15 | 143.547 | 25.238 |
| variable-cost | cost-aware | 25/26 | 15 | 143.602 | 25.199 |

## Real install/verification boundary

`benchmarks/check_scope_expansion_install.py` serves two small packages from its own localhost registry. The production manifest writer adds an exact missing peer, the production install runs npm, installed versions are compared with the assignment, and the unmocked authoritative verifier runs a five-second runtime check. The fixture passed with non-empty resolved-state and preparation-proof keys. This validates that boundary; it does not exercise a real customer project, external agent, vulnerability audit, or publication.

## Reproduce

```powershell
python benchmarks/bench_greedy_cohorts.py --delay-seconds 0 --reps 4 --ratios 2 --steps 128 --strategies whole-first,split-first,size-aware,cost-aware
python benchmarks/bench_greedy_cohorts.py --delay-seconds 5 --reps 1 --ratios 2 --steps 128 --strategies whole-first,split-first,size-aware,cost-aware
python benchmarks/bench_greedy_cohorts.py --delay-seconds 5 --reps 1 --ratios 2 --steps 128 --profiles mandatory-companion,variable-cost --strategies whole-first,size-aware,cost-aware
python benchmarks/check_scope_expansion_install.py
```

Fixture projects, per-attempt histories and JSON/Markdown summaries are retained under `.dependency-roadmap/qa/`. Demo failures never affect another project. Agent duration is reported as unavailable rather than zero.

## Verification record

`python run_tool_tests.py --suite all`: 1714 tests, four skipped, one failing inventory check for the new Desktop check missing from CI. Added the actual CI invocation and reran the inventory: two tests passed. The complete suite was not repeated after that YAML fix or the final adjustment that records success costs after the durable checkpoint pointer switch; materialization/retry (5), schema (33) and physical iterative acceptance (1) tests were rerun for that adjustment and passed.

`python run_tool_tests.py --suite production-fast`: 64 tests passed. Desktop lint/build, iterative agent/runner/scenario/migration/begin/checkout and repair handoff/dispatch checks passed. Lint and build retain existing warnings; no acceptance checks were disabled.

Commands for the additional gates (root unless noted):

```powershell
python run_tool_tests.py --suite production-fast
python -m unittest discover -s tests/regression -p test_desktop_check_inventory.py -v
python -m unittest discover -s tests/regression -p test_iterative_scope_expansion.py -v
python -m unittest discover -s tests/regression -p test_cohort_action_cost.py -v
python -m unittest discover -s tests/regression -p test_iterative_engine_precheck.py -v
python -m unittest discover -s tests/regression -p test_iterative_materialize_retry_budget.py -v
python -m unittest discover -s tests/regression -p test_iterative_migration_schemas.py -v
python -m unittest discover -s tests/acceptance -p test_iterative_migration_physical.py -v
# From desktop/
npm run lint
npm run build
npm run check:iterative-checkout
npm run check:iterative-agent
npm run check:iterative-runner
npm run check:iterative-scenario
npm run check:iterative-migration
npm run check:iterative-begin
npm run check:repair-handoff
npm run check:repair-dispatch
npm run check:i18n
```

Browser QA: in-app Browser, local scope-expansion harness, 1280×720, RU/EN labels and Open folder callback checked; no overlay or console errors on the final clean tab. OS Explorer launch and other viewports were not exercised by the browser harness.
