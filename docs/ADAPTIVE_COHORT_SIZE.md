# Adaptive cohort size: implementation and measured result

Current follow-up: [local conflict regions](LOCAL_CONFLICT_REGIONS.md) preserve the whole-first policy and add local opaque-failure fallback. The global adaptive measurements below remain historical evidence; global adaptive-size is not the production default.

Implemented an opt-in adaptive-size scheduler. Initial size/cap is 24 unless explicitly configured. First definitive physical failure allows switching to another intact cohort; the second consecutive failure halves the soft size. Two verified successes using the full current size increase it by 25% (at least one package), up to the cap. Tiny remainder successes do not inflate the size. UNKNOWN, infrastructure failures, unavailable target versions and agent prose do not vote as project incompatibility.

Dependency atoms and mandatory companions remain intact when the soft limit shrinks. An atom may exceed that soft limit but must fit the explicit hard cap; oversized atoms are rejected with an actionable cap/proposal error. Soft family and installed metadata hints are not promoted to compatibility proof.

**Decision: keep production default whole-first.** The global adaptive prototype is available with --cohort-scheduling-strategy adaptive-size, but the first comparison does not justify promoting it. cost-aware is benchmark-only; physical migration rejects that experimental strategy. Ratio/search budget remain conservative defaults, with bounded escalation only on structural exhaustion.

## Comparison

120 virtual runs: 96 targets, four matched seeds, six scenarios, fixed-8/fixed-24/current whole-first/current size-aware/adaptive-size. Same cap, targets and oracle. Each check costs 120 modeled seconds, rejection recovery 60 modeled seconds. Conflicts are exact clauses in sparse/dense fixtures; opaque-failure exposes only success/failure and the exact rejected assignment. Reachable coverage is known only in the fixture. It does not enter candidate selection or create production proof. Measurement stops when that oracle maximum is reached; real completion requires its own checks and evidence.

| Scenario | Policy | Accepted | First checkpoint, min | 25%, min | 50%, min | 75%, min | 90%, min | Failed wall, min | Completion, min |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| clean | fixed-8 | 96/96 | 2.00 | 6.00 | 12.00 | 18.00 | 22.00 | 0.00 | 24.00 |
| clean | fixed-24 | 96/96 | 2.00 | 2.00 | 4.00 | 6.00 | 8.00 | 0.00 | 8.00 |
| clean | whole-first | 96/96 | 2.00 | 2.00 | 4.00 | 6.00 | 8.00 | 0.00 | 8.00 |
| clean | size-aware | 96/96 | 2.00 | 2.00 | 4.00 | 6.00 | 8.00 | 0.00 | 8.00 |
| clean | adaptive-size | 96/96 | 2.00 | 2.00 | 4.00 | 6.00 | 8.00 | 0.00 | 8.00 |
| cross-conflicts | fixed-8 | 72/96 | 2.00 | 8.25 | 26.50 | 45.00 | 58.00 | 40.50 | 68.50 |
| cross-conflicts | fixed-24 | 72/96 | 6.50 | 9.25 | 20.75 | 27.25 | 31.50 | 24.75 | 35.75 |
| cross-conflicts | whole-first | 72/96 | 6.50 | 9.25 | 20.75 | 27.25 | 31.50 | 24.75 | 35.75 |
| cross-conflicts | size-aware | 72/96 | 6.50 | 9.25 | 20.75 | 27.25 | 31.50 | 24.75 | 35.75 |
| cross-conflicts | adaptive-size | 72/96 | 5.00 | 6.75 | 18.25 | 39.75 | 51.25 | 34.50 | 66.50 |
| dense-conflicts | fixed-8 | 81/96 | 8.00 | 19.50 | 42.00 | 58.00 | 67.00 | 40.50 | 72.00 |
| dense-conflicts | fixed-24 | 81/96 | 9.50 | 16.50 | 43.50 | 44.50 | 47.50 | 37.50 | 48.50 |
| dense-conflicts | whole-first | 81/96 | 9.50 | 16.50 | 43.50 | 44.50 | 47.50 | 37.50 | 48.50 |
| dense-conflicts | size-aware | 81/96 | 9.50 | 16.50 | 43.50 | 44.50 | 47.50 | 37.50 | 48.50 |
| dense-conflicts | adaptive-size | 81/96 | 6.50 | 13.00 | 35.00 | 55.00 | 66.75 | 33.75 | 79.75 |
| large-remainder | fixed-8 | 95/96 | 2.00 | 7.50 | 17.25 | 30.00 | 38.50 | 31.50 | 59.50 |
| large-remainder | fixed-24 | 95/96 | 2.75 | 2.75 | 6.25 | 10.50 | 23.50 | 24.00 | 38.50 |
| large-remainder | whole-first | 95/96 | 2.75 | 2.75 | 6.25 | 10.50 | 23.50 | 24.00 | 38.50 |
| large-remainder | size-aware | 95/96 | 2.75 | 2.75 | 6.25 | 10.50 | 23.50 | 24.00 | 38.50 |
| large-remainder | adaptive-size | 95/96 | 2.75 | 2.75 | 6.25 | 10.50 | 23.50 | 24.00 | 38.50 |
| mandatory-companion | fixed-8 | 96/96 | 5.00 | 9.00 | 15.00 | 21.00 | 25.00 | 3.00 | 27.00 |
| mandatory-companion | fixed-24 | 96/96 | 5.00 | 5.00 | 7.00 | 9.00 | 11.00 | 3.00 | 11.00 |
| mandatory-companion | whole-first | 96/96 | 5.00 | 5.00 | 7.00 | 9.00 | 11.00 | 3.00 | 11.00 |
| mandatory-companion | size-aware | 96/96 | 5.00 | 5.00 | 7.00 | 9.00 | 11.00 | 3.00 | 11.00 |
| mandatory-companion | adaptive-size | 96/96 | 5.00 | 5.00 | 7.00 | 9.00 | 11.00 | 3.00 | 11.00 |
| opaque-failure | fixed-8 | 72/96 | 2.00 | 9.75 | 36.25 | 233.50 | 634.00 | 1254.00 | 1297.50 |
| opaque-failure | fixed-24 | 72/96 | 6.50 | 10.75 | 52.00 | 357.25 | 865.25 | 1484.25 | 1524.75 |
| opaque-failure | whole-first | 72/96 | 6.50 | 10.75 | 52.00 | 357.25 | 865.25 | 1484.25 | 1524.75 |
| opaque-failure | size-aware | 72/96 | 6.50 | 10.00 | 51.25 | 353.50 | 861.50 | 1548.00 | 1588.50 |
| opaque-failure | adaptive-size | 72/96 | 5.00 | 11.75 | 66.50 | 335.75 | 784.50 | 1121.25 | 1204.25 |

The hypothesis is only partly supported. Adaptive size keeps clean/companion/remainder results here and reaches the first checkpoint earlier in sparse/dense cases, but total completion regresses substantially when exact conflict clauses are available. Opaque failures improve, yet all simple policies spend very large modeled time finding the final compatible assignment. A larger initial size and faster first incumbent are distinct benefits; neither establishes faster completion.

Likely limitation of this first controller: global failure history shrinks unrelated work and cautious growth requires full-sized successful cohorts. Exact structural filtering often returns smaller covers, delaying recovery of the global size. A future controller should consider the affected region and actual failure stage after real telemetry, rather than introducing another parameter sweep now.

JSON retains progressCurve, per-attempt history, timeToReachableCoverageSeconds, failedWallSeconds, coverageGap and completionState. Unknown or incomplete completion is not given a fast completion time. All 120 runs reached their known fixture maximum. No real-project speedup is claimed.

## Physical telemetry

cohort-attempts.jsonl and ledger.attemptTelemetry record operator, package/family/atom/companion count, PASS/FAIL/UNKNOWN, failure stage, per-command duration, accepted delta, cumulative verified goal targets and completed physical actions without verified progress. ledger.cohortSizeState persists the controller across CLI processes and restarts, scoped by run/project/policy/runtime/validation. A duplicate event does not spend another controller vote. Successful checkpoint telemetry is written after the durable pointer switch; the next physical stage reconciles progress if telemetry was missed during a crash.

Stages include install, tsc, lint, build, tests, generic project-check, verification preparation, audit and agent. Missing timings are null, never zero. Command labels supply stage classification; original command and verifier events remain evidence. Rows distinguish schedulingStrategy and suggestedCohortSize from nextCohortSize; the latter is populated only when adaptive-size is actually selected. Agent completion stays UNKNOWN for project validity. Provider session wall span comes from the Desktop-owned lease; cohort-agent-telemetry.jsonl also records dispatch span including waits/feedback/failures. These overlapping spans are explicitly scoped and must not be summed as independent stage costs. Scripted repairs do not claim actual provider-agent time.

## Three owned physical fixture projects

Each uses a localhost package registry, real npm resolution/install, exact installed-version observation and the authoritative verifier. All source guards remain enabled. The TypeScript compiler comes from the existing Desktop dependencies. These tiny projects exercise physical boundaries and stage timing, not migration performance on a customer repository.

- runtime-tests: install 1.38s, verification/re-verification 14.17s, stages install:PASS, preparation:PASS, tests:PASS.

- typed-build: install 1.60s, verification/re-verification 19.30s, stages install:PASS, preparation:PASS, tsc:PASS, build:PASS, project-check:PASS.

- opaque-repair: install 1.68s, verification/re-verification 31.03s, stages preparation:PASS, tsc:PASS, build:PASS, project-check:PASS.
  The actual compiler rejected the initial source. A scripted source edit was followed by fresh physical verification of the repaired bytes; no solver conflict clause was inferred from the compiler output.

An initial fixture run failed SOURCE_IDENTITY_MISMATCH because its telemetry file was created inside the sealed source project. Moved only fixture telemetry outside the source tree and reran; did not weaken the ordinary source guard.

## Reproduce

```powershell
python -X utf8 benchmarks/bench_greedy_cohorts.py --virtual-time --delay-seconds 120 --packages 96 --reps 4 --ratios 2 --steps 128 --strategies fixed-8,fixed-24,whole-first,size-aware,adaptive-size --profiles clean,cross-conflicts,dense-conflicts,large-remainder,mandatory-companion,opaque-failure --recovery-seconds 60
python -X utf8 benchmarks/check_scope_expansion_install.py --profile runtime-tests
python -X utf8 benchmarks/check_scope_expansion_install.py --profile typed-build
python -X utf8 benchmarks/check_scope_expansion_install.py --profile opaque-repair
```

Raw synthetic results: ignored .dependency-roadmap/qa/greedy-tuning/20261006-000155. Physical results and verification event streams remain under the owned .dependency-roadmap/qa/ directories. Source HEAD before and after implementation: 8d76803ed20004dc6ab860856e1aa9304af65472 with local changes; no publication or customer migration was performed.

## Validation record

- `python -X utf8 -m unittest discover -s tests/regression -p test_adaptive_cohort_size.py -v`: final 18 tests passed, including checkpoint reconciliation, malformed evidence, atomic shrink and inconclusive outcomes.
- Final narrow suites: `test_iterative_migration_schemas.py` 33 passed; `test_iterative_materialize_retry_budget.py` 5 passed; `test_iterative_scope_expansion.py` 14 passed; `test_verified_baseline_fast_budget.py` 20 passed.
- `python -X utf8 -m unittest discover -s tests/acceptance -p test_iterative_migration_physical.py -v`: one real filesystem/Git/npm/verifier migration passed, including cumulative accepted telemetry and rejected source repair. Run before the final malformed-event guards and observability labels; those final edits were covered by the 18-test suite.
- `python -X utf8 run_tool_tests.py --suite production-fast`: 64 passed. Run before final telemetry guards/labels.
- Desktop: `npm run lint`, `npm run build`, final `npx tsc -p tsconfig.electron.json`, `npm run check:iterative-agent` and `npm run check:iterative-runner` passed. Existing lint/build warnings remain.
- `python -X utf8 run_tool_tests.py --suite all`: 1728 tests ran, four skipped, one failure and one error. This run began before the last small edits. The failure was a size-aware test relying on the previous implicit default; it now selects size-aware explicitly. The error was a Windows subprocess encoding mismatch; the test now pins UTF-8 for child output and decoding. Both complete affected modules passed their fresh targeted reruns above. The full suite was not repeated after these fixes and is not reported as a green full gate.
- Sandbox-only Windows Temp access failures in targeted reruns were resolved by approved test reruns. No application checks or acceptance thresholds were weakened.
- `git diff --check` passed. Implementation is local and unpublished.
